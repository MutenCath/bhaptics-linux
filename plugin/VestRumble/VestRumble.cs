// VestRumble — generic haptics profile for Unity games (bhaptics-linux).
//
// Hooks the handful of APIs Unity games use to rumble controllers/gamepads
// and mirrors those events to the bhaptics-linux daemon as chest taps
// (OSC /vest/tap on udp://127.0.0.1:9001). No game files are modified;
// all hooks are in-memory Harmony postfixes.
//
// GPL-3.0-or-later, part of bhaptics-linux.
using System;
using System.Collections.Generic;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using BepInEx;
using BepInEx.Configuration;
using BepInEx.Logging;
using HarmonyLib;

namespace VestRumble
{
    [BepInPlugin("org.bhaptics-linux.vestrumble", "VestRumble", "0.1.0")]
    public class Plugin : BaseUnityPlugin
    {
        internal static ManualLogSource Log;
        static UdpClient udp;
        static ConfigEntry<float> intensity;
        static ConfigEntry<int> minMs;
        static readonly Dictionary<string, DateTime> lastSend = new Dictionary<string, DateTime>();

        void Awake()
        {
            Log = Logger;
            intensity = Config.Bind("General", "Intensity", 1.0f,
                "Vest tap strength multiplier (0..2)");
            minMs = Config.Bind("General", "MinIntervalMs", 70,
                "Minimum milliseconds between taps per side");
            try
            {
                udp = new UdpClient();
                udp.Connect("127.0.0.1", 9001);
            }
            catch (Exception e)
            {
                Log.LogWarning("VestRumble: UDP setup failed: " + e.Message);
            }

            var h = new Harmony("org.bhaptics-linux.vestrumble");
            int n = 0;
            // SteamVR Input system (most Unity VR games since ~2019)
            n += TryPatch(h, "Valve.VR.SteamVR_Action_Vibration", "Execute", nameof(PostVibrationExecute));
            // legacy SteamVR plugin (pre-Input-system era)
            n += TryPatch(h, "SteamVR_Controller+Device", "TriggerHapticPulse", nameof(PostLegacyPulse));
            // Oculus integration
            n += TryPatch(h, "OVRInput", "SetControllerVibration", nameof(PostOvrVibration));
            // Unity XR plugin framework
            n += TryPatch(h, "UnityEngine.XR.InputDevice", "SendHapticImpulse", nameof(PostXrImpulse));
            // flat games: new Input System gamepad rumble
            n += TryPatch(h, "UnityEngine.InputSystem.Gamepad", "SetMotorSpeeds", nameof(PostPadMotors));
            Log.LogInfo("VestRumble: hooked " + n + " haptic API(s)");
        }

        static Type FindType(string name)
        {
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                try
                {
                    var t = asm.GetType(name, false);
                    if (t != null) return t;
                }
                catch { }
            }
            return null;
        }

        int TryPatch(Harmony h, string typeName, string methodName, string postfixName)
        {
            var t = FindType(typeName);
            if (t == null) return 0;
            var pf = new HarmonyMethod(typeof(Plugin).GetMethod(
                postfixName, BindingFlags.Static | BindingFlags.NonPublic));
            int n = 0;
            foreach (var m in t.GetMethods(BindingFlags.Public | BindingFlags.NonPublic
                                           | BindingFlags.Instance | BindingFlags.Static))
            {
                if (m.Name != methodName || m.IsAbstract) continue;
                try
                {
                    h.Patch(m, postfix: pf);
                    n++;
                }
                catch (Exception e)
                {
                    Log.LogDebug("VestRumble: cannot patch " + typeName + "." + methodName
                                 + ": " + e.Message);
                }
            }
            if (n > 0) Log.LogInfo("VestRumble: hooked " + typeName + "." + methodName);
            return n > 0 ? 1 : 0;
        }

        // ---- postfixes ------------------------------------------------

        // Execute(float secondsFromNow, float durationSeconds, float frequency,
        //         float amplitude, SteamVR_Input_Sources inputSource)
        static void PostVibrationExecute(object[] __args)
        {
            if (__args == null || __args.Length < 5) return;
            Tap(SideFromName(Str(__args[4])), ToF(__args[3]), MsFromSec(ToF(__args[1])));
        }

        // TriggerHapticPulse(ushort durationMicroSec, EVRButtonId buttonId)
        static void PostLegacyPulse(object[] __args)
        {
            float usec = (__args != null && __args.Length > 0) ? ToF(__args[0]) : 500f;
            Tap("both", usec / 3999f, 80);
        }

        // SetControllerVibration(float frequency, float amplitude, Controller mask)
        static void PostOvrVibration(object[] __args)
        {
            if (__args == null || __args.Length < 3) return;
            Tap(SideFromName(Str(__args[2])), ToF(__args[1]), 100);
        }

        // InputDevice.SendHapticImpulse(uint channel, float amplitude, float duration)
        static void PostXrImpulse(object __instance, object[] __args)
        {
            if (__args == null || __args.Length < 2) return;
            string side = "both";
            try
            {
                var p = __instance.GetType().GetProperty("characteristics")
                        ?? __instance.GetType().GetProperty("role");
                if (p != null) side = SideFromName(Str(p.GetValue(__instance, null)));
            }
            catch { }
            float dur = __args.Length > 2 ? ToF(__args[2]) : 0.1f;
            Tap(side, ToF(__args[1]), MsFromSec(dur));
        }

        // Gamepad.SetMotorSpeeds(float lowFrequency, float highFrequency)
        static void PostPadMotors(object[] __args)
        {
            if (__args == null || __args.Length < 2) return;
            Tap("both", Math.Max(ToF(__args[0]), ToF(__args[1])), 110);
        }

        // ---- helpers ---------------------------------------------------

        static void Tap(string side, float amp, int ms)
        {
            if (udp == null) return;
            amp *= intensity.Value;
            if (amp <= 0.03f) return;
            if (amp > 1f) amp = 1f;
            var now = DateTime.UtcNow;
            DateTime last;
            if (lastSend.TryGetValue(side, out last)
                    && (now - last).TotalMilliseconds < minMs.Value)
                return;
            lastSend[side] = now;
            try
            {
                var pkt = Osc("/vest/tap", side, amp, ms);
                udp.Send(pkt, pkt.Length);
            }
            catch { }
        }

        static byte[] Osc(string addr, string s, float f, int i)
        {
            var b = new List<byte>();
            PadStr(b, addr);
            PadStr(b, ",sfi");
            PadStr(b, s);
            var fb = BitConverter.GetBytes(f);
            if (BitConverter.IsLittleEndian) Array.Reverse(fb);
            b.AddRange(fb);
            var ib = BitConverter.GetBytes(i);
            if (BitConverter.IsLittleEndian) Array.Reverse(ib);
            b.AddRange(ib);
            return b.ToArray();
        }

        static void PadStr(List<byte> b, string s)
        {
            b.AddRange(Encoding.ASCII.GetBytes(s));
            b.Add(0);
            while (b.Count % 4 != 0) b.Add(0);
        }

        static string Str(object o) { return o == null ? "" : o.ToString(); }

        static float ToF(object o)
        {
            try { return Convert.ToSingle(o); } catch { return 0f; }
        }

        static int MsFromSec(float s)
        {
            int ms = (int)(s * 1000f);
            return ms < 40 ? 80 : (ms > 1000 ? 1000 : ms);
        }

        static string SideFromName(string n)
        {
            n = (n ?? "").ToLowerInvariant();
            bool l = n.Contains("left") || n.Contains("ltouch");
            bool r = n.Contains("right") || n.Contains("rtouch");
            if (l && !r) return "left";
            if (r && !l) return "right";
            return "both";
        }
    }
}
