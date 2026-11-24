/*
 * bhaptics_xr_layer.c — OpenXR implicit API layer (proof of concept)
 *
 * Captures xrApplyHapticFeedback / xrStopHapticFeedback from every OpenXR
 * application (including Windows VR games under Proton: wineopenxr.so loads
 * the native Linux OpenXR loader, so native implicit layers are seen by
 * Windows games too) and mirrors each vibration as an OSC 1.0 packet over
 * UDP to the bhaptics-linux daemon on 127.0.0.1:9001:
 *
 *     /vest/tap  <side: "left"|"right"|"both">  <amp: float 0..1>  <ms: int32>
 *
 * Behavior notes:
 *  - Always calls through to the next layer / loader and returns its result
 *    unchanged. A send failure can never affect the game.
 *  - UDP sendto on a non-blocking socket, best effort, never blocks.
 *  - Logs to stderr only when the BHAPTICS_XR_LOG environment variable is set.
 *  - No pthreads; a fixed 4-slot per-instance table keeps dispatch correct
 *    for up to 4 concurrent XrInstances in one process (games use one).
 *
 * License: GPL-3.0-or-later (matches the bhaptics-linux repository).
 * Vendored Khronos headers: vendor/openxr (Apache-2.0, see NOTICE).
 */
#define _GNU_SOURCE /* SOCK_NONBLOCK */
#include <openxr/openxr.h>
#include <openxr/openxr_loader_negotiation.h>

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define BHAPTICS_LAYER_NAME "XR_APILAYER_NOVENDOR_bhaptics_capture"
#define BHAPTICS_OSC_ADDRESS "/vest/tap"
#define BHAPTICS_OSC_PORT 9001
#define BHAPTICS_LOG_ENV "BHAPTICS_XR_LOG"
#define BHAPTICS_MAX_INSTANCES 4
#define BHAPTICS_UNBOUNDED_MS 2000

/* One slot per XrInstance the layer is chained into. */
typedef struct BhapticsInstance {
    int inUse;
    XrInstance instance;
    PFN_xrGetInstanceProcAddr nextGipa;
    PFN_xrApplyHapticFeedback nextApplyHaptic;
    PFN_xrStopHapticFeedback nextStopHaptic;
} BhapticsInstance;

static BhapticsInstance g_instances[BHAPTICS_MAX_INSTANCES];
static int g_sockfd = -1; /* shared UDP socket, opened lazily, never closed (PoC) */

/* ------------------------------------------------------------------ */
/* logging (env-gated)                                                */
/* ------------------------------------------------------------------ */
static int bhaptics_logging(void) {
    static int cached = 0, enabled = 0;
    if (!cached) {
        enabled = getenv(BHAPTICS_LOG_ENV) != NULL;
        cached = 1;
    }
    return enabled;
}
#define LOGF(...)                                        \
    do {                                                 \
        if (bhaptics_logging())                          \
            fprintf(stderr, "bhaptics-layer: " __VA_ARGS__); \
    } while (0)

/* ------------------------------------------------------------------ */
/* per-instance bookkeeping                                           */
/* ------------------------------------------------------------------ */
static BhapticsInstance *bhaptics_find(XrInstance instance) {
    for (int i = 0; i < BHAPTICS_MAX_INSTANCES; ++i) {
        if (g_instances[i].inUse && g_instances[i].instance == instance)
            return &g_instances[i];
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* OSC 1.0 encoding (big-endian, 4-byte aligned)                      */
/* ------------------------------------------------------------------ */
static size_t osc_put_string(uint8_t *buf, const char *s) {
    size_t len = strlen(s) + 1;
    size_t pad = (4u - (len & 3u)) & 3u;
    memcpy(buf, s, len);
    memset(buf + len, 0, pad);
    return len + pad;
}

static size_t osc_put_u32(uint8_t *buf, uint32_t v) {
    v = htonl(v); /* OSC is big-endian */
    memcpy(buf, &v, sizeof(v));
    return sizeof(v);
}

static size_t osc_put_f32(uint8_t *buf, float f) {
    union {
        float f;
        uint32_t u;
    } bits = {f};
    return osc_put_u32(buf, bits.u);
}

/* "/vest/tap", ",sfi", side(string), amplitude(float), durationMs(int32) */
static size_t bhaptics_build_tap(uint8_t *buf, const char *side, float amp, int32_t durMs) {
    size_t off = osc_put_string(buf, BHAPTICS_OSC_ADDRESS);
    off += osc_put_string(buf + off, ",sfi");
    off += osc_put_string(buf + off, side);
    off += osc_put_f32(buf + off, amp);
    off += osc_put_u32(buf + off, (uint32_t)durMs);
    return off;
}

/* ------------------------------------------------------------------ */
/* UDP, best effort                                                   */
/* ------------------------------------------------------------------ */
static void bhaptics_send(const uint8_t *packet, size_t len) {
    if (g_sockfd < 0) {
        int fd = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
        if (fd < 0) {
            LOGF("socket() failed: %s\n", strerror(errno));
            return;
        }
        /* Benign race (no pthreads by design): two threads racing here each
         * open a socket and one fd leaks. Harmless for a PoC. */
        g_sockfd = fd;
    }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(BHAPTICS_OSC_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (sendto(g_sockfd, packet, len, 0, (const struct sockaddr *)&addr, sizeof(addr)) < 0)
        LOGF("sendto() failed: %s\n", strerror(errno));
}

/* ------------------------------------------------------------------ */
/* haptic -> OSC mapping                                              */
/* ------------------------------------------------------------------ */
static const char *bhaptics_side(BhapticsInstance *it, XrPath path) {
    if (path == XR_NULL_PATH)
        return "both";
    PFN_xrPathToString pathToString = NULL;
    if (it->nextGipa(it->instance, "xrPathToString", (PFN_xrVoidFunction *)&pathToString) != XR_SUCCESS ||
        pathToString == NULL)
        return "both";
    char buf[XR_MAX_PATH_LENGTH];
    if (pathToString(it->instance, path, sizeof(buf), NULL, buf) != XR_SUCCESS)
        return "both";
    if (strstr(buf, "left") != NULL)
        return "left";
    if (strstr(buf, "right") != NULL)
        return "right";
    return "both";
}

/* XrDuration is int64 nanoseconds. 1.1 defines "unbounded" as duration 0
 * (1.0's XR_UNBOUNDED_DURATION was also 0). */
static int32_t bhaptics_duration_ms(XrDuration ns) {
    if (ns == 0)
        return BHAPTICS_UNBOUNDED_MS;
    if (ns < 0)
        return 0;
    int64_t ms = ns / 1000000;
    if (ms > INT32_MAX)
        ms = INT32_MAX;
    return (int32_t)ms;
}

/* Negative amplitude is the amplitude-sentinel convention: treat as 0.5. */
static float bhaptics_amplitude(float a) {
    if (a < 0.0f)
        a = 0.5f;
    if (a > 1.0f)
        a = 1.0f;
    return a;
}

/* ------------------------------------------------------------------ */
/* hooked core functions                                              */
/* ------------------------------------------------------------------ */
static XrResult bhaptics_apply_impl(BhapticsInstance *it, XrSession session,
                                    const XrHapticActionInfo *actionInfo,
                                    const XrHapticBaseHeader *baseHeader) {
    if (actionInfo != NULL && baseHeader != NULL && baseHeader->type == XR_TYPE_HAPTIC_VIBRATION) {
        const XrHapticVibration *vib = (const XrHapticVibration *)baseHeader;
        const char *side = bhaptics_side(it, actionInfo->subactionPath);
        float amp = bhaptics_amplitude(vib->amplitude);
        int32_t ms = bhaptics_duration_ms(vib->duration);
        uint8_t packet[256];
        size_t len = bhaptics_build_tap(packet, side, amp, ms);
        bhaptics_send(packet, len);
        LOGF("xrApplyHapticFeedback side=%s amp=%.3f ms=%d\n", side, (double)amp, ms);
    }
    /* Always call through and return the next layer's result unchanged. */
    if (it->nextApplyHaptic == NULL)
        return XR_ERROR_HANDLE_INVALID;
    return it->nextApplyHaptic(session, actionInfo, baseHeader);
}

static XrResult bhaptics_stop_impl(BhapticsInstance *it, XrSession session,
                                   const XrHapticActionInfo *actionInfo) {
    LOGF("xrStopHapticFeedback\n");
    if (it->nextStopHaptic == NULL)
        return XR_ERROR_HANDLE_INVALID;
    return it->nextStopHaptic(session, actionInfo);
}

/* The hooked functions do not receive an XrInstance (only a session), so
 * per-instance data cannot be looked up inside them. A small function pool
 * — one pair of hooks hard-bound to each slot — keeps dispatch correct for
 * up to BHAPTICS_MAX_INSTANCES instances without a global "current" chain. */
#define BHAPTICS_DEFINE_HOOKS(idx)                                                        \
    static XrResult XRAPI_CALL bhaptics_apply_hook_##idx(XrSession session,               \
                                                         const XrHapticActionInfo *actionInfo,  \
                                                         const XrHapticBaseHeader *baseHeader) { \
        return bhaptics_apply_impl(&g_instances[idx], session, actionInfo, baseHeader);   \
    }                                                                                     \
    static XrResult XRAPI_CALL bhaptics_stop_hook_##idx(XrSession session,                \
                                                        const XrHapticActionInfo *actionInfo) { \
        return bhaptics_stop_impl(&g_instances[idx], session, actionInfo);                \
    }
BHAPTICS_DEFINE_HOOKS(0)
BHAPTICS_DEFINE_HOOKS(1)
BHAPTICS_DEFINE_HOOKS(2)
BHAPTICS_DEFINE_HOOKS(3)

static const PFN_xrApplyHapticFeedback bhaptics_apply_hooks[BHAPTICS_MAX_INSTANCES] = {
    bhaptics_apply_hook_0, bhaptics_apply_hook_1, bhaptics_apply_hook_2, bhaptics_apply_hook_3};
static const PFN_xrStopHapticFeedback bhaptics_stop_hooks[BHAPTICS_MAX_INSTANCES] = {
    bhaptics_stop_hook_0, bhaptics_stop_hook_1, bhaptics_stop_hook_2, bhaptics_stop_hook_3};

/* ------------------------------------------------------------------ */
/* loader interface                                                   */
/* ------------------------------------------------------------------ */
static XrResult XRAPI_CALL bhaptics_layer_gipa(XrInstance instance, const char *name,
                                               PFN_xrVoidFunction *function) {
    if (strcmp(name, "xrApplyHapticFeedback") == 0 || strcmp(name, "xrStopHapticFeedback") == 0) {
        BhapticsInstance *it = bhaptics_find(instance);
        if (it == NULL)
            return XR_ERROR_HANDLE_INVALID; /* pre-instance: let the loader fall through */
        size_t idx = (size_t)(it - g_instances);
        *function = (strcmp(name, "xrApplyHapticFeedback") == 0)
                        ? (PFN_xrVoidFunction)bhaptics_apply_hooks[idx]
                        : (PFN_xrVoidFunction)bhaptics_stop_hooks[idx];
        return XR_SUCCESS;
    }
    BhapticsInstance *it = bhaptics_find(instance);
    if (it == NULL)
        return XR_ERROR_HANDLE_INVALID; /* nothing else is ours; fall through */
    return it->nextGipa(instance, name, function);
}

static XrResult XRAPI_CALL bhaptics_layer_cali(const XrInstanceCreateInfo *info,
                                               const XrApiLayerCreateInfo *apiLayerInfo,
                                               XrInstance *instance) {
    /* The classic bug: the nextInfo chain head describes our callee, so we
     * must advance it past that entry before calling down, or the next
     * layer sees its own entry and calls itself. */
    XrApiLayerCreateInfo chainInfo = *apiLayerInfo;
    chainInfo.nextInfo = apiLayerInfo->nextInfo->next;

    XrResult result = apiLayerInfo->nextInfo->nextCreateApiLayerInstance(info, &chainInfo, instance);
    if (XR_FAILED(result))
        return result;

    PFN_xrGetInstanceProcAddr nextGipa = apiLayerInfo->nextInfo->nextGetInstanceProcAddr;
    PFN_xrGetInstanceProcAddr instanceGipa = nextGipa;
    PFN_xrVoidFunction fn = NULL;
    if (nextGipa(*instance, "xrGetInstanceProcAddr", &fn) == XR_SUCCESS && fn != NULL)
        instanceGipa = (PFN_xrGetInstanceProcAddr)fn; /* prefer per-instance gipa */

    BhapticsInstance *slot = NULL;
    for (int i = 0; i < BHAPTICS_MAX_INSTANCES; ++i) {
        if (!g_instances[i].inUse) {
            slot = &g_instances[i];
            break;
        }
    }
    if (slot == NULL) {
        /* Instance already exists; degrade gracefully instead of failing it. */
        LOGF("instance table full (%d); haptics capture disabled\n", BHAPTICS_MAX_INSTANCES);
        return XR_SUCCESS;
    }
    slot->inUse = 1;
    slot->instance = *instance;
    slot->nextGipa = instanceGipa;
    slot->nextApplyHaptic = NULL;
    slot->nextStopHaptic = NULL;
    instanceGipa(*instance, "xrApplyHapticFeedback", (PFN_xrVoidFunction *)&slot->nextApplyHaptic);
    instanceGipa(*instance, "xrStopHapticFeedback", (PFN_xrVoidFunction *)&slot->nextStopHaptic);

    /* Not present in the OpenXR 1.1.43 loader, but install our per-instance
     * gipa under the layer name when a future loader provides the call. */
    typedef XrResult(XRAPI_PTR * PFN_xrSetInstanceProcAddr)(XrInstance, const char *, PFN_xrVoidFunction);
    PFN_xrVoidFunction setFn = NULL;
    if (instanceGipa(*instance, "xrSetInstanceProcAddr", &setFn) == XR_SUCCESS && setFn != NULL)
        ((PFN_xrSetInstanceProcAddr)setFn)(*instance, BHAPTICS_LAYER_NAME,
                                           (PFN_xrVoidFunction)bhaptics_layer_gipa);
    return XR_SUCCESS;
}

XRAPI_ATTR XrResult XRAPI_CALL xrNegotiateLoaderApiLayerInterface(
    const XrNegotiateLoaderInfo *loaderInfo, const char *layerName,
    XrNegotiateApiLayerRequest *apiLayerRequest) {
    (void)layerName;
    if (loaderInfo == NULL || apiLayerRequest == NULL)
        return XR_ERROR_INITIALIZATION_FAILED;
    if (loaderInfo->structVersion != XR_LOADER_INFO_STRUCT_VERSION)
        return XR_ERROR_INITIALIZATION_FAILED;
    if (loaderInfo->minInterfaceVersion > XR_CURRENT_LOADER_API_LAYER_VERSION ||
        loaderInfo->maxInterfaceVersion < XR_CURRENT_LOADER_API_LAYER_VERSION)
        return XR_ERROR_INITIALIZATION_FAILED;

    apiLayerRequest->structType = XR_LOADER_INTERFACE_STRUCT_API_LAYER_REQUEST;
    apiLayerRequest->structVersion = XR_API_LAYER_INFO_STRUCT_VERSION;
    apiLayerRequest->structSize = sizeof(XrNegotiateApiLayerRequest);
    apiLayerRequest->layerInterfaceVersion = XR_CURRENT_LOADER_API_LAYER_VERSION;
    apiLayerRequest->layerApiVersion = XR_MAKE_VERSION(1, 0, 0); /* needs core 1.0 only */
    apiLayerRequest->getInstanceProcAddr = bhaptics_layer_gipa;
    apiLayerRequest->createApiLayerInstance = bhaptics_layer_cali;
    LOGF("negotiated as %s (interface %u, api %d.%d.%d)\n", BHAPTICS_LAYER_NAME,
         XR_CURRENT_LOADER_API_LAYER_VERSION, 1, 0, 0);
    return XR_SUCCESS;
}
