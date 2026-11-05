// nvmon-window.exe - the frameless window `nvmon app` shows the dashboard in.
//
// A WinForms form with no title bar hosting WebView2 (the Edge engine that
// ships with Windows 10/11). Windows 11 rounds its corners and draws its
// shadow; the dashboard's own header is the title bar: pressing empty
// header space posts "drag" and the host runs Windows' own move loop
// (Snap works), double-click posts "max". A 5 px margin around the page
// is the resize grip. (CSS app-region does not reach a windowed WebView2's
// parent, so it is not used.)
//
// The page talks to it with window.chrome.webview.postMessage(cmd):
//   drag | collapse | min | max | close | pin | opacity | state
// `collapse` hides the window entirely; posting WM_APP+5 (0x8005) to it
// brings it back (that is what nvmon app's floating widget does).
// and gets {"pinned": bool, "opacity": n, "maximised": bool} back.
//
// Args:  --url <url> --data <dir> [--title nvmon]
// <dir> keeps the WebView2 profile and window.txt (bounds, pin, opacity).
//
// Build: build.bat next to this file (csc.exe from .NET Framework 4, so
// C# 5: no string interpolation, no ?. operator).

using System;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

static class Program
{
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr v);

    [STAThread]
    static int Main(string[] args)
    {
        string url = null, data = null, title = "nvmon";
        for (int i = 0; i + 1 < args.Length; i += 2)
        {
            if (args[i] == "--url") url = args[i + 1];
            else if (args[i] == "--data") data = args[i + 1];
            else if (args[i] == "--title") title = args[i + 1];
        }
        if (url == null || data == null)
        {
            MessageBox.Show("usage: nvmon-window --url <url> --data <dir>", "nvmon");
            return 2;
        }
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (Exception) { }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new HostForm(url, data, title));
        return 0;
    }
}

class HostForm : Form
{
    const int WM_NCCALCSIZE = 0x0083, WM_NCHITTEST = 0x0084, WM_APP_SHOW = 0x8005;
    const int HTCLIENT = 1, HTLEFT = 10, HTRIGHT = 11, HTTOP = 12, HTTOPLEFT = 13,
              HTTOPRIGHT = 14, HTBOTTOM = 15, HTBOTTOMLEFT = 16, HTBOTTOMRIGHT = 17;
    static readonly double[] OpacitySteps = { 1.0, 0.95, 0.9, 0.8, 0.7 };
    static readonly Color PageBg = Color.FromArgb(10, 10, 10);

    [StructLayout(LayoutKind.Sequential)]
    struct RECT { public int Left, Top, Right, Bottom; }

    [DllImport("dwmapi.dll")]
    static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int i);
    [DllImport("user32.dll")] static extern bool ReleaseCapture();
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, IntPtr l);
    const int WM_NCLBUTTONDOWN = 0x00A1, HTCAPTION = 2;
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);

    readonly string url, dataDir, stateFile;
    readonly WebView2 web;
    bool pinned;
    double opacity = 0.95;
    int grip = 5;

    public HostForm(string url, string dataDir, string title)
    {
        this.url = url;
        this.dataDir = dataDir;
        this.stateFile = Path.Combine(dataDir, "window.txt");
        Text = title;
        FormBorderStyle = FormBorderStyle.None;
        BackColor = PageBg;
        StartPosition = FormStartPosition.Manual;
        MinimumSize = new Size(480, 300);
        try
        {
            string ico = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "nvmon.ico");
            if (File.Exists(ico)) Icon = new Icon(ico);
        }
        catch (Exception) { }

        web = new WebView2();
        web.Dock = DockStyle.Fill;
        web.DefaultBackgroundColor = PageBg;
        Controls.Add(web);

        LoadState();
        Load += OnLoadAsync;
        Resize += delegate { UpdateGrip(); };
        FormClosing += delegate { SaveState(); };
    }

    // A normal resizable, minimisable window to Windows (snap, animations,
    // rounded corners, shadow) whose non-client area is sized to zero below.
    protected override CreateParams CreateParams
    {
        get
        {
            CreateParams cp = base.CreateParams;
            cp.Style |= 0x00040000 | 0x00C00000 | 0x00020000 | 0x00010000 | 0x00080000;
            return cp; // THICKFRAME | CAPTION | MINIMIZEBOX | MAXIMIZEBOX | SYSMENU
        }
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        int v = 2;          // DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
        DwmSetWindowAttribute(Handle, 33, ref v, 4);
        v = 1;              // DWMWA_USE_IMMERSIVE_DARK_MODE
        DwmSetWindowAttribute(Handle, 20, ref v, 4);
        v = 0x00262626;     // DWMWA_BORDER_COLOR: subtle dark hairline (0x00BBGGRR)
        DwmSetWindowAttribute(Handle, 34, ref v, 4);
        ApplyOpacity();
        TopMost = pinned;
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_APP_SHOW)
        {
            // From `nvmon app` (widget clicked): come back from collapsed
            // (hidden) or minimised, in front.
            Show();
            if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
            Activate();
            return;
        }
        if (m.Msg == WM_NCCALCSIZE && m.WParam != IntPtr.Zero)
        {
            if (WindowState == FormWindowState.Maximized)
            {
                // A maximised window overhangs the monitor by its frame;
                // pull the client back inside the work area.
                RECT r = (RECT)Marshal.PtrToStructure(m.LParam, typeof(RECT));
                int f = Frame();
                r.Left += f; r.Top += f; r.Right -= f; r.Bottom -= f;
                Marshal.StructureToPtr(r, m.LParam, false);
            }
            m.Result = IntPtr.Zero;   // no title bar, no borders
            return;
        }
        if (m.Msg == WM_NCHITTEST && WindowState != FormWindowState.Maximized)
        {
            Point p = PointToClient(new Point(unchecked((short)(long)m.LParam),
                                              unchecked((short)((long)m.LParam >> 16))));
            int g = grip + 2, w = ClientSize.Width, h = ClientSize.Height;
            bool l = p.X < g, r = p.X >= w - g, t = p.Y < g, b = p.Y >= h - g;
            int hit = t && l ? HTTOPLEFT : t && r ? HTTOPRIGHT : b && l ? HTBOTTOMLEFT
                    : b && r ? HTBOTTOMRIGHT : l ? HTLEFT : r ? HTRIGHT : t ? HTTOP
                    : b ? HTBOTTOM : HTCLIENT;
            m.Result = new IntPtr(hit);
            return;
        }
        base.WndProc(ref m);
    }

    int Frame()
    {
        uint dpi = 96;
        try { dpi = GetDpiForWindow(Handle); } catch (Exception) { }
        // SM_CXSIZEFRAME + SM_CXPADDEDBORDER, scaled for the window's monitor
        return (int)Math.Round((GetSystemMetrics(32) + GetSystemMetrics(92)) * Math.Max(dpi, 96u) / 96.0);
    }

    void UpdateGrip()
    {
        uint dpi = 96;
        try { dpi = GetDpiForWindow(Handle); } catch (Exception) { }
        grip = (int)Math.Round(5 * dpi / 96.0);
        Padding = WindowState == FormWindowState.Maximized ? Padding.Empty : new Padding(grip);
    }

    async void OnLoadAsync(object sender, EventArgs e)
    {
        UpdateGrip();
        try
        {
            CoreWebView2Environment env = await CoreWebView2Environment.CreateAsync(
                null, Path.Combine(dataDir, "webview2"), null);
            await web.EnsureCoreWebView2Async(env);
        }
        catch (Exception ex)
        {
            MessageBox.Show("WebView2 could not start:\n\n" + ex.Message +
                            "\n\nInstall the Microsoft Edge WebView2 Runtime.", "nvmon");
            Environment.Exit(3);
            return;
        }
        CoreWebView2 core = web.CoreWebView2;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.AreBrowserAcceleratorKeysEnabled = true;
        core.DocumentTitleChanged += delegate { Text = core.DocumentTitle; };
        core.NewWindowRequested += delegate (object s, CoreWebView2NewWindowRequestedEventArgs a)
        {
            a.Handled = true;   // links that open a new window go to the real browser
            try { Process.Start(a.Uri); } catch (Exception) { }
        };
        core.WebMessageReceived += OnMessage;
        core.Navigate(url);
    }

    void OnMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        string cmd;
        try { cmd = e.TryGetWebMessageAsString(); } catch (Exception) { return; }
        if (Environment.GetEnvironmentVariable("NVMON_WINDOW_DEBUG") == "1")
        {
            try { File.AppendAllText(Path.Combine(dataDir, "debug.log"), DateTime.Now.ToString("HH:mm:ss.fff ") + cmd + "\r\n"); }
            catch (Exception) { }
        }
        switch (cmd)
        {
            case "drag":
                // Mouse is down on empty header space: hand it to Windows'
                // own move loop as if the title bar was pressed (Snap,
                // multi-monitor, drag-out-of-maximised all work).
                ReleaseCapture();
                SendMessage(Handle, WM_NCLBUTTONDOWN, new IntPtr(HTCAPTION), IntPtr.Zero);
                return;
            case "collapse":
                // Hide completely (no taskbar button): `nvmon app` shows its
                // floating widget instead and sends WM_APP_SHOW to bring us back.
                SaveState();
                Hide();
                return;
            case "min": WindowState = FormWindowState.Minimized; break;
            case "max":
                WindowState = WindowState == FormWindowState.Maximized
                    ? FormWindowState.Normal : FormWindowState.Maximized;
                break;
            case "close": Close(); return;
            case "pin": pinned = !pinned; TopMost = pinned; SaveState(); break;
            case "opacity":
                double next = OpacitySteps[0];
                foreach (double o in OpacitySteps) { if (o < opacity - 0.001) { next = o; break; } }
                opacity = next;
                ApplyOpacity();
                SaveState();
                break;
        }
        web.CoreWebView2.PostWebMessageAsJson(string.Format(CultureInfo.InvariantCulture,
            "{{\"pinned\":{0},\"opacity\":{1},\"maximised\":{2}}}",
            pinned ? "true" : "false", opacity,
            WindowState == FormWindowState.Maximized ? "true" : "false"));
    }

    void ApplyOpacity()
    {
        // Form.Opacity makes the form a layered window; at 100 % drop it
        // again so the page renders exactly as in a browser.
        Opacity = opacity >= 0.999 ? 1.0 : opacity;
    }

    // window.txt: "x y w h maximised pinned opacity"
    void LoadState()
    {
        Rectangle wa = Screen.PrimaryScreen.WorkingArea;
        int w = Math.Min(1280, wa.Width - 80), h = Math.Min(760, wa.Height - 80);
        Bounds = new Rectangle(wa.Left + (wa.Width - w) / 2, wa.Top + (wa.Height - h) / 2, w, h);
        try
        {
            string[] p = File.ReadAllText(stateFile).Trim().Split(' ');
            Rectangle r = new Rectangle(int.Parse(p[0]), int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]));
            foreach (Screen s in Screen.AllScreens)
            {
                if (s.WorkingArea.IntersectsWith(new Rectangle(r.X + 40, r.Y + 10, 120, 40)))
                {
                    Bounds = r;
                    break;
                }
            }
            if (p[4] == "1") WindowState = FormWindowState.Maximized;
            pinned = p[5] == "1";
            opacity = double.Parse(p[6], CultureInfo.InvariantCulture);
        }
        catch (Exception) { }
    }

    void SaveState()
    {
        try
        {
            Directory.CreateDirectory(dataDir);
            Rectangle r = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
            File.WriteAllText(stateFile, string.Format(CultureInfo.InvariantCulture,
                "{0} {1} {2} {3} {4} {5} {6}", r.X, r.Y, r.Width, r.Height,
                WindowState == FormWindowState.Maximized ? 1 : 0, pinned ? 1 : 0, opacity));
        }
        catch (Exception) { }
    }
}
