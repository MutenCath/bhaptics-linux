/*
 * test_layer.c — harness for libXrApiLayer_bhaptics.so
 *
 * Acts as a mock OpenXR loader (negotiation) plus a fake "next layer"
 * dispatch table, and verifies:
 *   (a) xrNegotiateLoaderApiLayerInterface succeeds and reports the expected
 *       interface version;
 *   (b) the layer's xrApplyHapticFeedback passes arguments through to the
 *       next layer and returns its sentinel result unchanged;
 *   (c) a UDP listener on 127.0.0.1:9001 receives a parseable OSC packet
 *       with the right address and args;
 *   (d) xrStopHapticFeedback passes through.
 */
#include <openxr/openxr.h>
#include <openxr/openxr_loader_negotiation.h>

#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#define LAYER_SO "./libXrApiLayer_bhaptics.so"
#define LAYER_NAME "XR_APILAYER_NOVENDOR_bhaptics_capture"
#define OSC_PORT 9001
#define SENTINEL ((XrResult)0x7D2ACE11)

static int g_failures = 0;

#define CHECK(cond, msg)                                                   \
    do {                                                                   \
        if (!(cond)) {                                                     \
            fprintf(stderr, "FAIL: %s (line %d)\n", (msg), __LINE__);      \
            g_failures++;                                                  \
        } else {                                                           \
            printf("ok: %s\n", (msg));                                     \
        }                                                                  \
    } while (0)

/* ------------------------------------------------------------------ */
/* mock "next layer" (stands in for the real loader/runtime)          */
/* ------------------------------------------------------------------ */
typedef struct Mock {
    XrResult applyResult;
    XrResult stopResult;
    int applyCalled;
    int stopCalled;
    int chainAdvanceOk;
    XrSession lastSession;
    XrAction lastAction;
    XrPath lastSubactionPath;
    XrStructureType lastHapticType;
    XrDuration lastDuration;
    float lastFrequency;
    float lastAmplitude;
} Mock;

static Mock g_mock;

static XrResult XRAPI_CALL mock_apply(XrSession session, const XrHapticActionInfo *a,
                                      const XrHapticBaseHeader *h) {
    const XrHapticVibration *v = (const XrHapticVibration *)h;
    g_mock.applyCalled++;
    g_mock.lastSession = session;
    g_mock.lastAction = a->action;
    g_mock.lastSubactionPath = a->subactionPath;
    g_mock.lastHapticType = h->type;
    g_mock.lastDuration = v->duration;
    g_mock.lastFrequency = v->frequency;
    g_mock.lastAmplitude = v->amplitude;
    return g_mock.applyResult;
}

static XrResult XRAPI_CALL mock_stop(XrSession session, const XrHapticActionInfo *a) {
    g_mock.stopCalled++;
    g_mock.lastSession = session;
    g_mock.lastAction = a->action;
    return g_mock.stopResult;
}

static XrResult XRAPI_CALL mock_path_to_string(XrInstance instance, XrPath path,
                                               uint32_t cap, uint32_t *count, char *buffer) {
    (void)instance;
    const char *s = (path == 2) ? "/user/hand/right" : "/user/hand/left";
    if (count != NULL)
        *count = (uint32_t)strlen(s) + 1;
    if (buffer != NULL && cap >= strlen(s) + 1)
        memcpy(buffer, s, strlen(s) + 1);
    return XR_SUCCESS;
}

static XrResult XRAPI_CALL mock_gipa(XrInstance instance, const char *name,
                                     PFN_xrVoidFunction *function) {
    (void)instance;
    if (strcmp(name, "xrGetInstanceProcAddr") == 0) {
        *function = (PFN_xrVoidFunction)mock_gipa;
        return XR_SUCCESS;
    }
    if (strcmp(name, "xrApplyHapticFeedback") == 0) {
        *function = (PFN_xrVoidFunction)mock_apply;
        return XR_SUCCESS;
    }
    if (strcmp(name, "xrStopHapticFeedback") == 0) {
        *function = (PFN_xrVoidFunction)mock_stop;
        return XR_SUCCESS;
    }
    if (strcmp(name, "xrPathToString") == 0) {
        *function = (PFN_xrVoidFunction)mock_path_to_string;
        return XR_SUCCESS;
    }
    return XR_ERROR_FUNCTION_UNSUPPORTED;
}

/* The mock loader's own chain entry. Convention (Khronos loader / template):
 * the chain head a layer receives describes the layer's CALLEE, so with one
 * layer above us the layer must call mock_cali with nextInfo advanced to NULL. */
static XrResult XRAPI_CALL mock_cali(const XrInstanceCreateInfo *info,
                                     const XrApiLayerCreateInfo *apiLayerInfo,
                                     XrInstance *instance) {
    (void)info;
    g_mock.chainAdvanceOk = (apiLayerInfo->nextInfo == NULL);
    *instance = (XrInstance)(uintptr_t)0xCAFE0001;
    return XR_SUCCESS;
}

/* ------------------------------------------------------------------ */
/* OSC parsing helpers (mirror the layer's encoding)                  */
/* ------------------------------------------------------------------ */
static const char *osc_next_string(const uint8_t *p, size_t n, size_t *off) {
    const char *s = (const char *)(p + *off);
    size_t len = strlen(s) + 1;
    *off += (len + 3u) & ~3u;
    if (*off > n)
        *off = n;
    return s;
}

static float osc_next_f32(const uint8_t *p, size_t n, size_t *off) {
    uint32_t u;
    if (*off + 4 > n)
        return 0.0f;
    memcpy(&u, p + *off, 4);
    *off += 4;
    u = ntohl(u);
    float f;
    memcpy(&f, &u, 4);
    return f;
}

static int32_t osc_next_i32(const uint8_t *p, size_t n, size_t *off) {
    uint32_t u;
    if (*off + 4 > n)
        return 0;
    memcpy(&u, p + *off, 4);
    *off += 4;
    return (int32_t)ntohl(u);
}

/* ------------------------------------------------------------------ */
/* ------------------------------------------------------------------ */
static int udp_listener(void) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0)
        return -1;
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(OSC_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(fd, (const struct sockaddr *)&addr, sizeof(addr)) != 0) {
        fprintf(stderr, "FAIL: cannot bind UDP 127.0.0.1:%d: %s\n", OSC_PORT, strerror(errno));
        close(fd);
        return -1;
    }
    struct timeval tv = {2, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    return fd;
}

static int recv_osc(int fd, uint8_t *packet, size_t cap, ssize_t *outLen) {
    ssize_t n = recv(fd, packet, cap, 0);
    *outLen = n;
    return n > 0;
}

/* Expects: /vest/tap ,sfi <side> <amp f32> <ms i32>, all 36 bytes
 * (12 addr + 8 typetag + 8 side + 4 float + 4 int). */
static void check_osc_packet(const uint8_t *p, ssize_t n, const char *wantSide,
                             float wantAmp, int32_t wantMs, const char *label) {
    char msg[256];
    snprintf(msg, sizeof(msg), "(c) OSC packet %s: 36 bytes", label);
    CHECK(n == 36, msg);
    if (n != 36)
        return;
    size_t off = 0;
    const char *addr = osc_next_string(p, (size_t)n, &off);
    snprintf(msg, sizeof(msg), "(c) OSC packet %s: address /vest/tap", label);
    CHECK(strcmp(addr, "/vest/tap") == 0, msg);
    const char *tag = osc_next_string(p, (size_t)n, &off);
    snprintf(msg, sizeof(msg), "(c) OSC packet %s: typetag ,sfi", label);
    CHECK(strcmp(tag, ",sfi") == 0, msg);
    const char *side = osc_next_string(p, (size_t)n, &off);
    snprintf(msg, sizeof(msg), "(c) OSC packet %s: side %s", label, wantSide);
    CHECK(strcmp(side, wantSide) == 0, msg);
    float amp = osc_next_f32(p, (size_t)n, &off);
    snprintf(msg, sizeof(msg), "(c) OSC packet %s: amplitude %.3f", label, (double)wantAmp);
    CHECK(amp == wantAmp, msg);
    int32_t ms = osc_next_i32(p, (size_t)n, &off);
    snprintf(msg, sizeof(msg), "(c) OSC packet %s: duration %d ms", label, wantMs);
    CHECK(ms == wantMs, msg);
}

/* ------------------------------------------------------------------ */
int main(void) {
    void *so = dlopen(LAYER_SO, RTLD_NOW | RTLD_LOCAL);
    CHECK(so != NULL, "dlopen layer .so");
    if (so == NULL) {
        fprintf(stderr, "dlerror: %s\n", dlerror());
        return 1;
    }

    PFN_xrNegotiateLoaderApiLayerInterface negotiate = NULL;
    *(void **)(&negotiate) = dlsym(so, "xrNegotiateLoaderApiLayerInterface");
    CHECK(negotiate != NULL, "dlsym xrNegotiateLoaderApiLayerInterface");
    if (negotiate == NULL)
        return 1;

    /* ---- (a) negotiation ---- */
    XrNegotiateLoaderInfo loaderInfo = {
        .structType = XR_LOADER_INTERFACE_STRUCT_LOADER_INFO,
        .structVersion = XR_LOADER_INFO_STRUCT_VERSION,
        .structSize = sizeof(loaderInfo),
        .minInterfaceVersion = 1,
        .maxInterfaceVersion = XR_CURRENT_LOADER_API_LAYER_VERSION,
        .minApiVersion = XR_MAKE_VERSION(1, 0, 0),
        .maxApiVersion = XR_CURRENT_API_VERSION,
    };
    XrNegotiateApiLayerRequest req;
    memset(&req, 0, sizeof(req));
    XrResult r = negotiate(&loaderInfo, LAYER_NAME, &req);
    CHECK(r == XR_SUCCESS, "(a) negotiation succeeds");
    CHECK(req.layerInterfaceVersion == XR_CURRENT_LOADER_API_LAYER_VERSION,
          "(a) reports expected interface version");
    CHECK(req.getInstanceProcAddr != NULL && req.createApiLayerInstance != NULL,
          "(a) getInstanceProcAddr and createApiLayerInstance present");
    if (r != XR_SUCCESS)
        return 1;

    /* ---- chain setup, as the real loader would for one layer: the head
       entry the layer receives describes its callee (the mock loader). ---- */
    XrApiLayerNextInfo loaderEntry = {
        .structType = XR_LOADER_INTERFACE_STRUCT_API_LAYER_NEXT_INFO,
        .structVersion = XR_API_LAYER_NEXT_INFO_STRUCT_VERSION,
        .structSize = sizeof(loaderEntry),
        .layerName = "",
        .nextGetInstanceProcAddr = mock_gipa,
        .nextCreateApiLayerInstance = mock_cali,
        .next = NULL,
    };
    XrApiLayerCreateInfo caliInfo = {
        .structType = XR_LOADER_INTERFACE_STRUCT_API_LAYER_CREATE_INFO,
        .structVersion = XR_API_LAYER_CREATE_INFO_STRUCT_VERSION,
        .structSize = sizeof(caliInfo),
        .loaderInstance = NULL,
        .settings_file_location = "",
        .nextInfo = &loaderEntry,
    };
    XrInstanceCreateInfo instInfo;
    memset(&instInfo, 0, sizeof(instInfo));
    instInfo.type = XR_TYPE_INSTANCE_CREATE_INFO;
    instInfo.applicationInfo.apiVersion = XR_MAKE_VERSION(1, 0, 0);

    XrInstance instance = XR_NULL_HANDLE;
    r = req.createApiLayerInstance(&instInfo, &caliInfo, &instance);
    CHECK(r == XR_SUCCESS, "xrCreateApiLayerInstance chains down successfully");
    CHECK(g_mock.chainAdvanceOk, "layer advanced the nextInfo chain before calling down");
    CHECK(instance == (XrInstance)(uintptr_t)0xCAFE0001, "instance handle comes from next layer");

    /* ---- (b) apply: sentinel passthrough ---- */
    PFN_xrApplyHapticFeedback apply = NULL;
    r = req.getInstanceProcAddr(instance, "xrApplyHapticFeedback", (PFN_xrVoidFunction *)&apply);
    CHECK(r == XR_SUCCESS && apply != NULL, "(b) gipa resolves xrApplyHapticFeedback");
    if (apply == NULL)
        return 1;

    /* Unknown names must chain to the next layer, not be swallowed. */
    PFN_xrVoidFunction bogus = NULL;
    r = req.getInstanceProcAddr(instance, "xrBeginSession", &bogus);
    CHECK(r == XR_ERROR_FUNCTION_UNSUPPORTED, "unknown names chain to next layer");

    int udp = udp_listener();
    CHECK(udp >= 0, "(c) UDP listener bound on 127.0.0.1:9001");
    if (udp < 0)
        return 1;

    XrHapticActionInfo hapticActionInfo = {.type = XR_TYPE_HAPTIC_ACTION_INFO,
                                           .next = NULL,
                                           .action = (XrAction)0x42,
                                           .subactionPath = (XrPath)1}; /* /user/hand/left */
    XrHapticVibration vibration = {.type = XR_TYPE_HAPTIC_VIBRATION,
                                   .next = NULL,
                                   .duration = 250000000, /* 250 ms */
                                   .frequency = 0.0f,
                                   .amplitude = 0.75f};
    g_mock.applyResult = SENTINEL;
    g_mock.applyCalled = 0;
    r = apply((XrSession)(uintptr_t)0xBEEF, &hapticActionInfo,
              (const XrHapticBaseHeader *)&vibration);
    CHECK(r == SENTINEL, "(b) apply returns next layer's sentinel unchanged");
    CHECK(g_mock.applyCalled == 1, "(b) next layer's apply was called");
    CHECK(g_mock.lastSession == (XrSession)(uintptr_t)0xBEEF &&
              g_mock.lastAction == (XrAction)0x42 && g_mock.lastSubactionPath == 1,
          "(b) session/action/subactionPath passed through");
    CHECK(g_mock.lastDuration == 250000000 && g_mock.lastAmplitude == 0.75f,
          "(b) haptic payload passed through unmodified");

    uint8_t packet[256];
    ssize_t n = 0;
    CHECK(recv_osc(udp, packet, sizeof(packet), &n), "(c) received UDP packet");
    check_osc_packet(packet, n, "left", 0.75f, 250, "left hand");

    /* Unbounded duration (0 ns) caps at 2000 ms; negative amplitude -> 0.5. */
    vibration.duration = 0;
    vibration.amplitude = -1.0f;
    hapticActionInfo.subactionPath = (XrPath)2; /* /user/hand/right */
    g_mock.applyCalled = 0;
    r = apply((XrSession)(uintptr_t)0xBEEF, &hapticActionInfo,
              (const XrHapticBaseHeader *)&vibration);
    CHECK(r == SENTINEL && g_mock.applyCalled == 1, "(b) second apply passes through");
    CHECK(recv_osc(udp, packet, sizeof(packet), &n), "(c) received second UDP packet");
    check_osc_packet(packet, n, "right", 0.5f, 2000, "unbounded/negative-amp");

    /* NULL subaction path -> "both"; amplitude clamps to 1.0. */
    vibration.duration = 100000000; /* 100 ms */
    vibration.amplitude = 1.5f;
    hapticActionInfo.subactionPath = XR_NULL_PATH;
    g_mock.applyCalled = 0;
    r = apply((XrSession)(uintptr_t)0xBEEF, &hapticActionInfo,
              (const XrHapticBaseHeader *)&vibration);
    CHECK(r == SENTINEL && g_mock.applyCalled == 1, "(b) third apply passes through");
    CHECK(recv_osc(udp, packet, sizeof(packet), &n), "(c) received third UDP packet");
    check_osc_packet(packet, n, "both", 1.0f, 100, "null-path/clamped");

    /* ---- (d) stop passthrough ---- */
    PFN_xrStopHapticFeedback stop = NULL;
    r = req.getInstanceProcAddr(instance, "xrStopHapticFeedback", (PFN_xrVoidFunction *)&stop);
    CHECK(r == XR_SUCCESS && stop != NULL, "(d) gipa resolves xrStopHapticFeedback");
    if (stop != NULL) {
        g_mock.stopResult = SENTINEL;
        g_mock.stopCalled = 0;
        hapticActionInfo.subactionPath = (XrPath)1;
        r = stop((XrSession)(uintptr_t)0xBEEF, &hapticActionInfo);
        CHECK(r == SENTINEL, "(d) stop returns next layer's sentinel unchanged");
        CHECK(g_mock.stopCalled == 1 && g_mock.lastAction == (XrAction)0x42,
              "(d) stop called through with action");
    }

    close(udp);
    dlclose(so);

    if (g_failures != 0) {
        fprintf(stderr, "%d FAILURES\n", g_failures);
        return 1;
    }
    printf("ALL TESTS PASSED\n");
    return 0;
}
