// Local-only Windows process-tree loopback probe. No microphone or network access.
// Build with the system .NET Framework x64 csc.exe. Capture is restricted to cloudmusic.
using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

[ComImport, Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioClient
{
    [PreserveSig] int Initialize(int mode, uint flags, long duration, long period, ref WaveFormat format, IntPtr session);
    [PreserveSig] int GetBufferSize(out uint frames);
    [PreserveSig] int GetStreamLatency(out long latency);
    [PreserveSig] int GetCurrentPadding(out uint frames);
    [PreserveSig] int IsFormatSupported(int mode, IntPtr format, out IntPtr closest);
    [PreserveSig] int GetMixFormat(out IntPtr format);
    [PreserveSig] int GetDevicePeriod(out long normal, out long minimum);
    [PreserveSig] int Start();
    [PreserveSig] int Stop();
    [PreserveSig] int Reset();
    [PreserveSig] int SetEventHandle(IntPtr handle);
    [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
}

[ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioCaptureClient
{
    [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
    [PreserveSig] int ReleaseBuffer(uint frames);
    [PreserveSig] int GetNextPacketSize(out uint frames);
}

[ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IActivateAudioInterfaceAsyncOperation
{
    [PreserveSig] int GetActivateResult(out int result, [MarshalAs(UnmanagedType.IUnknown)] out object activated);
}

[ComVisible(true), Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IActivateAudioInterfaceCompletionHandler
{
    [PreserveSig] int ActivateCompleted(IActivateAudioInterfaceAsyncOperation operation);
}

[ComVisible(true), Guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAgileObject { }

[ComVisible(true), ClassInterface(ClassInterfaceType.None)]
public sealed class ActivationHandler : IActivateAudioInterfaceCompletionHandler, IAgileObject
{
    public readonly ManualResetEvent Done = new ManualResetEvent(false);
    public object Activated;
    public int Error;
    public int ActivateCompleted(IActivateAudioInterfaceAsyncOperation operation)
    {
        try
        {
            int activationResult;
            int hr = operation.GetActivateResult(out activationResult, out Activated);
            Error = hr < 0 ? hr : activationResult;
        }
        catch (Exception ex) { Error = Marshal.GetHRForException(ex); }
        finally { Done.Set(); }
        return 0;
    }
}

[StructLayout(LayoutKind.Sequential, Pack = 2)]
public struct WaveFormat
{
    public ushort Tag, Channels;
    public uint SamplesPerSecond, AverageBytesPerSecond;
    public ushort BlockAlign, BitsPerSample, ExtraSize;
}

[StructLayout(LayoutKind.Sequential)]
public struct ActivationParams
{
    public int ActivationType;
    public uint TargetProcessId;
    public int ProcessLoopbackMode;
}

// This probe is compiled x64: PROPVARIANT's BLOB pointer is at offset 16.
[StructLayout(LayoutKind.Explicit, Size = 24)]
public struct PropVariant
{
    [FieldOffset(0)] public ushort Type;
    [FieldOffset(8)] public uint BlobSize;
    [FieldOffset(16)] public IntPtr BlobData;
}

public static class ProcessLoopback
{
    [DllImport("Mmdevapi.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int ActivateAudioInterfaceAsync(string device, ref Guid iid,
        ref PropVariant parameters, IActivateAudioInterfaceCompletionHandler handler,
        out IActivateAudioInterfaceAsyncOperation operation);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr GetStdHandle(int standardHandle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint GetFileType(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool WriteFile(IntPtr handle, IntPtr buffer, uint bytes,
        out uint written, IntPtr overlapped);

    private static void Check(int hr, string stage)
    {
        if (hr < 0) throw new COMException(stage + " failed: 0x" + hr.ToString("X8"), hr);
    }

    private static volatile bool stopRequested;

    private static void WriteHeader(BinaryWriter writer, uint bytes)
    {
        writer.Seek(0, SeekOrigin.Begin);
        writer.Write(Encoding.ASCII.GetBytes("RIFF")); writer.Write(checked(36u + bytes));
        writer.Write(Encoding.ASCII.GetBytes("WAVEfmt ")); writer.Write(16u);
        writer.Write((ushort)1); writer.Write((ushort)2); writer.Write(48000u);
        writer.Write(192000u); writer.Write((ushort)4); writer.Write((ushort)16);
        writer.Write(Encoding.ASCII.GetBytes("data")); writer.Write(bytes);
        writer.Flush();
    }

    private static void CheckOutputPipe()
    {
        // Framework's console stream can swallow broken-pipe errors. A zero-byte
        // native probe emits no protocol bytes but detects a reader/parent exit,
        // including during silence via heartbeat writes. No recorder is left running.
        IntPtr handle = GetStdHandle(-11);
        if (GetFileType(handle) != 3u) return; // FILE_TYPE_PIPE only; not files/consoles.
        uint written;
        if (!WriteFile(handle, IntPtr.Zero, 0, out written, IntPtr.Zero))
        {
            int error = Marshal.GetLastWin32Error();
            throw new IOException("Output pipe write failed: Win32 error " +
                error.ToString(CultureInfo.InvariantCulture) + ".");
        }
    }

    private static void WriteStreamHeader(BinaryWriter writer)
    {
        // BinaryWriter writes these numeric fields in little-endian order.
        writer.Write(Encoding.ASCII.GetBytes("NCM1"));
        writer.Write(48000u); writer.Write(2u); writer.Write(16u);
        writer.Flush();
        CheckOutputPipe();
    }

    private static void WriteStreamPacket(BinaryWriter writer, uint frames, uint flags,
        double receivedSeconds, byte[] pcm)
    {
        writer.Write(frames); writer.Write(flags); writer.Write(receivedSeconds);
        if (pcm != null) writer.Write(pcm);
        writer.Flush();
        CheckOutputPipe();
    }

    [MTAThread]
    public static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (args.Length != 3)
        {
            Console.Error.WriteLine("Usage: ProcessLoopback.exe <cloudmusic-pid> <seconds:1..30> <new-output.wav>");
            Console.Error.WriteLine("   or: ProcessLoopback.exe --stream <cloudmusic-pid> <seconds:0 or 1..3600>");
            return 2;
        }
        try
        {
            bool streamMode = string.Equals(args[0], "--stream", StringComparison.Ordinal);
            uint pid = uint.Parse(args[streamMode ? 1 : 0], CultureInfo.InvariantCulture);
            double seconds = double.Parse(args[streamMode ? 2 : 1], CultureInfo.InvariantCulture);
            if (double.IsNaN(seconds) || double.IsInfinity(seconds) ||
                (streamMode ? (seconds != 0 && (seconds < 1 || seconds > 3600)) :
                    (seconds < 1 || seconds > 30)))
                throw new ArgumentException(streamMode ?
                    "Duration must be 0 (until stopped) or between 1 and 3600 seconds." :
                    "Duration must be between 1 and 30 seconds.");
            using (Process target = Process.GetProcessById(checked((int)pid)))
            {
                if (!string.Equals(target.ProcessName, "cloudmusic", StringComparison.OrdinalIgnoreCase))
                    throw new ArgumentException("Only a cloudmusic process may be captured by this probe.");
            }
            if (IntPtr.Size != 8) throw new PlatformNotSupportedException("Use the x64 build.");
            return Capture(pid, seconds, streamMode ? null : Path.GetFullPath(args[2]), streamMode);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.GetType().Name + ": " + ex.Message);
            return 1;
        }
    }

    private static int Capture(uint pid, double seconds, string output, bool streamMode)
    {
        IAudioClient audio = null;
        IAudioCaptureClient capture = null;
        IActivateAudioInterfaceAsyncOperation operation = null;
        ActivationHandler handler = new ActivationHandler();
        IntPtr activationMemory = IntPtr.Zero;
        EventWaitHandle ready = new EventWaitHandle(false, EventResetMode.AutoReset);
        bool running = false;
        object captureGate = new object();
        ulong dataBytes = 0, packetCount = 0, discontinuities = 0, silentPackets = 0, timestampErrors = 0;
        double firstPacketMs = -1, maxPacketGapMs = 0, previousPacketMs = -1;
        Stopwatch clock = new Stopwatch();
        stopRequested = false;
        ConsoleCancelEventHandler cancelHandler = delegate(object sender, ConsoleCancelEventArgs e)
        {
            // A second Ctrl+C may terminate a helper whose output pipe is blocked.
            if (stopRequested) return;
            e.Cancel = true;
            stopRequested = true;
            // Never hold this gate during output: stop native capture even if stdout stalls.
            lock (captureGate)
            {
                if (running && audio != null)
                {
                    try
                    {
                        Check(audio.Stop(), "Stop");
                        running = false;
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine(ex.GetType().Name + ": " + ex.Message);
                    }
                }
            }
        };
        Console.CancelKeyPress += cancelHandler;
        try
        {
            ActivationParams parameters = new ActivationParams {
                ActivationType = 1, TargetProcessId = pid, ProcessLoopbackMode = 0
            };
            activationMemory = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(ActivationParams)));
            Marshal.StructureToPtr(parameters, activationMemory, false);
            PropVariant variant = new PropVariant {
                Type = 65, BlobSize = (uint)Marshal.SizeOf(typeof(ActivationParams)), BlobData = activationMemory
            };
            Guid clientId = typeof(IAudioClient).GUID;
            Check(ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ref clientId, ref variant,
                handler, out operation), "ActivateAudioInterfaceAsync");
            if (!handler.Done.WaitOne(10000))
            {
                // Native callback may still own the activation parameters. Exit the short-lived
                // helper instead of freeing memory beneath an in-flight callback.
                Console.Error.WriteLine("Audio activation timed out after 10 seconds.");
                Environment.Exit(3);
            }
            Check(handler.Error, "GetActivateResult");
            audio = (IAudioClient)handler.Activated;
            WaveFormat format = new WaveFormat {
                Tag = 1, Channels = 2, SamplesPerSecond = 48000,
                AverageBytesPerSecond = 192000, BlockAlign = 4, BitsPerSample = 16, ExtraSize = 0
            };
            Check(audio.Initialize(0, 0x00020000u | 0x00040000u | 0x80000000u,
                0, 0, ref format, IntPtr.Zero), "IAudioClient.Initialize");
            Check(audio.SetEventHandle(ready.SafeWaitHandle.DangerousGetHandle()), "SetEventHandle");
            Guid captureId = typeof(IAudioCaptureClient).GUID;
            object captureObject;
            Check(audio.GetService(ref captureId, out captureObject), "GetService");
            capture = (IAudioCaptureClient)captureObject;
            // Obtain raw stdout only after Main has set Console.OutputEncoding. There is
            // no file, background recorder, or separate native capture process in stream mode.
            using (BinaryWriter writer = new BinaryWriter(streamMode ? Console.OpenStandardOutput() :
                (Stream)new FileStream(output, FileMode.CreateNew, FileAccess.Write)))
            {
                if (streamMode) WriteStreamHeader(writer);
                else WriteHeader(writer, 0);
                try
                {
                    lock (captureGate)
                    {
                        if (!stopRequested)
                        {
                            Check(audio.Start(), "Start");
                            running = true;
                            clock.Start();
                        }
                    }
                    double lastStreamPacketSeconds = 0;
                    while (!stopRequested && (seconds == 0 || clock.Elapsed.TotalSeconds < seconds))
                    {
                        int waitMs = 100;
                        if (seconds != 0)
                            waitMs = Math.Min(waitMs, Math.Max(1,
                                (int)Math.Ceiling(seconds * 1000 - clock.Elapsed.TotalMilliseconds)));
                        if (streamMode)
                            waitMs = Math.Min(waitMs, Math.Max(1, (int)Math.Ceiling(
                                (lastStreamPacketSeconds + 0.250 - clock.Elapsed.TotalSeconds) * 1000)));
                        ready.WaitOne(waitMs);
                        while (!stopRequested && (seconds == 0 || clock.Elapsed.TotalSeconds < seconds))
                        {
                            uint frames, flags;
                            double receivedSeconds;
                            byte[] buffer;
                            lock (captureGate)
                            {
                                if (stopRequested) break;
                                uint available;
                                Check(capture.GetNextPacketSize(out available), "GetNextPacketSize");
                                if (available == 0) break;
                                IntPtr pointer;
                                ulong devicePosition, qpcPosition;
                                Check(capture.GetBuffer(out pointer, out frames, out flags,
                                    out devicePosition, out qpcPosition), "GetBuffer");
                                try
                                {
                                    // Receipt time since Start, NOT song position or speaker time.
                                    receivedSeconds = clock.Elapsed.TotalSeconds;
                                    buffer = new byte[checked((int)frames * 4)];
                                    // Only an explicit WASAPI SILENT packet represents zero PCM.
                                    // Missing packets/data must never be synthesized as silence.
                                    if ((flags & 2u) == 0 && buffer.Length != 0)
                                    {
                                        if (pointer == IntPtr.Zero)
                                            throw new InvalidDataException("Non-silent WASAPI packet has no PCM data.");
                                        Marshal.Copy(pointer, buffer, 0, buffer.Length);
                                    }
                                }
                                finally { Check(capture.ReleaseBuffer(frames), "ReleaseBuffer"); }
                            }
                            // The native buffer is already released before any potentially
                            // blocking pipe or WAV write (including the packet header).
                            double nowMs = receivedSeconds * 1000;
                            if (firstPacketMs < 0) firstPacketMs = nowMs;
                            if (previousPacketMs >= 0) maxPacketGapMs = Math.Max(maxPacketGapMs, nowMs - previousPacketMs);
                            previousPacketMs = nowMs;
                            if (streamMode)
                            {
                                WriteStreamPacket(writer, frames, flags & 7u, receivedSeconds, buffer);
                                lastStreamPacketSeconds = receivedSeconds;
                            }
                            else writer.Write(buffer);
                            checked
                            {
                                dataBytes += (uint)buffer.Length;
                                packetCount++;
                                if ((flags & 1u) != 0) discontinuities++;
                                if ((flags & 2u) != 0) silentPackets++;
                                if ((flags & 4u) != 0) timestampErrors++;
                            }
                        }
                        double elapsed = clock.Elapsed.TotalSeconds;
                        if (streamMode && !stopRequested && (seconds == 0 || elapsed < seconds) &&
                            elapsed - lastStreamPacketSeconds >= 0.250)
                        {
                            WriteStreamPacket(writer, 0, 0, elapsed, null);
                            lastStreamPacketSeconds = elapsed;
                        }
                    }
                }
                finally
                {
                    lock (captureGate)
                    {
                        if (running) { audio.Stop(); running = false; }
                        clock.Stop();
                    }
                    if (!streamMode) WriteHeader(writer, checked((uint)dataBytes));
                }
                // Exceptions (including a disconnected reader) stop capture via finally,
                // but must not produce a misleading normal-end packet.
                if (streamMode) WriteStreamPacket(writer, 0, 0x80000000u, clock.Elapsed.TotalSeconds, null);
            }
            TextWriter statistics = streamMode ? Console.Error : Console.Out;
            statistics.WriteLine(string.Format(CultureInfo.InvariantCulture,
                "{{\"target_pid\":{0},\"capture_scope\":\"process_tree_include\",\"wall_seconds\":{1:F4},\"audio_seconds\":{2:F4},\"packets\":{3},\"silent_packets\":{4},\"discontinuities\":{5},\"timestamp_errors\":{6},\"first_packet_ms\":{7:F3},\"max_packet_gap_ms\":{8:F3}",
                pid, clock.Elapsed.TotalSeconds, dataBytes / 192000.0, packetCount,
                silentPackets, discontinuities, timestampErrors, firstPacketMs, maxPacketGapMs) + "}");
            return 0;
        }
        finally
        {
            Console.CancelKeyPress -= cancelHandler;
            lock (captureGate)
            {
                if (running && audio != null) { audio.Stop(); running = false; }
            }
            if (capture != null) Marshal.ReleaseComObject(capture);
            if (audio != null) Marshal.ReleaseComObject(audio);
            if (operation != null) Marshal.ReleaseComObject(operation);
            if (activationMemory != IntPtr.Zero) Marshal.FreeHGlobal(activationMemory);
            ready.Dispose();
            handler.Done.Dispose();
            GC.KeepAlive(handler);
        }
    }
}

