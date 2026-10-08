// Program.cs — cchud-hook, the compiled Claude Code hook shim.
//
// A native-AOT rewrite of hookshim/cchud_hook.py. Same wire behaviour, but it
// starts in single-digit milliseconds and needs no runtime on the user's
// machine, so the settings.json command can point straight at the exe with no
// interpreter in the path.
//
// Constraints, in priority order, all inherited from the Python version:
//
//  1. Never block Claude Code. Every failure path exits 0 immediately.
//  2. Never wait on the network. UDP, not TCP: a TCP connect to a port with
//     nothing behind it does not fail fast when a VPN client or security suite
//     silently drops loopback SYNs, which cost this machine 266 ms per tool
//     call. A datagram cannot block.
//  3. Send a small payload. tool_input and tool_response can carry an entire
//     file's contents; the daemon never reads them, so they are not forwarded.
//
// Build:
//   dotnet publish -c Release -r win-x64 --self-contained true /p:PublishAot=true

using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;

const string Host = "127.0.0.1";
const int Port = 17321;
const int MaxDatagram = 1400;

// stdin is Claude Code's hook JSON. Read it once, in full, with a cap: a
// multi-megabyte payload would otherwise be buffered just to be discarded.
const int StdinMax = 1 << 20;

try
{
    using var stdin = Console.OpenStandardInput();
    var buffer = new byte[StdinMax];
    var total = 0;
    int read;
    while (total < StdinMax &&
           (read = stdin.Read(buffer, total, StdinMax - total)) > 0)
    {
        total += read;
    }

    if (total == 0)
    {
        return 0;
    }

    using var doc = JsonDocument.Parse(
        new ReadOnlyMemory<byte>(buffer, 0, total));
    var root = doc.RootElement;

    if (root.ValueKind != JsonValueKind.Object)
    {
        return 0;
    }

    if (!root.TryGetProperty("hook_event_name", out var eventEl) ||
        eventEl.ValueKind != JsonValueKind.String)
    {
        return 0;
    }

    var eventName = eventEl.GetString();
    if (string.IsNullOrEmpty(eventName))
    {
        return 0;
    }

    // Reduce before sending. Only the fields the daemon actually consumes.
    var payload = new StringBuilder(128);
    payload.Append("{\"v\":1,\"ev\":");
    AppendJsonString(payload, eventName);
    payload.Append(",\"tool\":");
    if (root.TryGetProperty("tool_name", out var toolEl) &&
        toolEl.ValueKind == JsonValueKind.String)
    {
        AppendJsonString(payload, toolEl.GetString());
    }
    else
    {
        payload.Append("null");
    }
    payload.Append(",\"sid\":");
    if (root.TryGetProperty("session_id", out var sidEl) &&
        sidEl.ValueKind == JsonValueKind.String)
    {
        AppendJsonString(payload, sidEl.GetString());
    }
    else
    {
        payload.Append("null");
    }
    payload.Append(",\"src\":\"cchud-hook\"}");

    var bytes = Encoding.UTF8.GetBytes(payload.ToString());
    if (bytes.Length > MaxDatagram)
    {
        return 0;
    }

    // No connect(): a connected UDP socket inherits the same filtering delay
    // this whole design exists to avoid.
    using var udp = new UdpClient();
    await udp.SendAsync(bytes, new IPEndPoint(IPAddress.Parse(Host), Port));
}
catch
{
    // Nothing here is worth failing a turn over.
}

return 0;

// Minimal JSON string escaping. Avoids pulling in a serializer and keeps the
// output dependency-free and byte-identical to the Python shim's json.dumps
// with separators=(",", ":").
static void AppendJsonString(StringBuilder sb, string? value)
{
    if (value is null)
    {
        sb.Append("null");
        return;
    }

    sb.Append('"');
    foreach (var ch in value)
    {
        switch (ch)
        {
            case '"':  sb.Append("\\\""); break;
            case '\\': sb.Append("\\\\"); break;
            case '\b': sb.Append("\\b");  break;
            case '\f': sb.Append("\\f");  break;
            case '\n': sb.Append("\\n");  break;
            case '\r': sb.Append("\\r");  break;
            case '\t': sb.Append("\\t");  break;
            default:
                if (ch < 0x20)
                {
                    sb.Append("\\u").Append(((int)ch).ToString("x4"));
                }
                else
                {
                    sb.Append(ch);
                }
                break;
        }
    }
    sb.Append('"');
}
