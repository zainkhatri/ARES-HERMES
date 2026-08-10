import SwiftUI
import WebKit
import Photos

// MARK: - Server Config

struct Server: Identifiable {
    let id: String
    let name: String
    let tag: String
    let letter: String
    let color: Color
    let urls: [String]
    var resolvedURL: String?
    var online: Bool = false
}

let HERMES_CANDIDATES = [
    "http://100.100.29.36:8080",
    "http://10.0.1.90:8080",
]

let ARES_CANDIDATES = [
    "http://100.77.42.110:8080",
]

// MARK: - Main View

struct MainView: View {
    @State private var servers: [Server] = [
        Server(id: "hermes", name: "HERMES", tag: "MID-NAS", letter: "H",
               color: Color(red: 0.39, green: 0.58, blue: 0.93), urls: HERMES_CANDIDATES),
        Server(id: "ares", name: "ARES", tag: "SUPER-NAS", letter: "A",
               color: Color(red: 0.94, green: 0.27, blue: 0.27), urls: ARES_CANDIDATES),
    ]
    @State private var selectedTab = "ares"
    @State private var discovering = true

    var body: some View {
        ZStack {
            Color(red: 0.016, green: 0.027, blue: 0.051)
                .ignoresSafeArea()

            if discovering {
                DiscoveryView(servers: $servers, discovering: $discovering, selectedTab: $selectedTab)
            } else {
                VStack(spacing: 0) {
                    // WebView area
                    ZStack {
                        ForEach(servers) { server in
                            if let url = server.resolvedURL, server.online {
                                NASWebView(urlString: url)
                                    .opacity(selectedTab == server.id ? 1 : 0)
                                    .allowsHitTesting(selectedTab == server.id)
                            }
                        }

                        // Offline state for selected server
                        if let sel = servers.first(where: { $0.id == selectedTab }), !sel.online {
                            VStack(spacing: 16) {
                                Text(sel.letter)
                                    .font(.system(size: 36, weight: .black))
                                    .foregroundColor(sel.color.opacity(0.3))
                                    .frame(width: 60, height: 60)
                                    .overlay(
                                        Rectangle().stroke(sel.color.opacity(0.2), lineWidth: 1)
                                    )
                                Text("\(sel.name) OFFLINE")
                                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                                    .foregroundColor(Color(white: 0.3))
                                    .tracking(3)
                                Button("Retry") {
                                    discovering = true
                                }
                                .font(.system(size: 11, weight: .medium, design: .monospaced))
                                .foregroundColor(sel.color)
                                .padding(.horizontal, 20)
                                .padding(.vertical, 8)
                                .overlay(Rectangle().stroke(sel.color.opacity(0.4), lineWidth: 1))
                            }
                        }
                    }

                    // Tab bar
                    HStack(spacing: 0) {
                        ForEach(servers) { server in
                            Button(action: {
                                withAnimation(.easeInOut(duration: 0.2)) {
                                    selectedTab = server.id
                                }
                            }) {
                                VStack(spacing: 5) {
                                    HStack(spacing: 6) {
                                        Text(server.letter)
                                            .font(.system(size: 12, weight: .black))
                                            .foregroundColor(
                                                selectedTab == server.id ? server.color : Color(white: 0.3)
                                            )
                                            .frame(width: 20, height: 20)
                                            .overlay(
                                                Rectangle()
                                                    .stroke(
                                                        selectedTab == server.id ? server.color.opacity(0.5) : Color(white: 0.15),
                                                        lineWidth: 1
                                                    )
                                            )

                                        VStack(alignment: .leading, spacing: 1) {
                                            Text(server.name)
                                                .font(.system(size: 11, weight: .bold, design: .monospaced))
                                                .foregroundColor(
                                                    selectedTab == server.id ? .white : Color(white: 0.35)
                                                )
                                            Text(server.tag)
                                                .font(.system(size: 8, weight: .medium, design: .monospaced))
                                                .foregroundColor(Color(white: 0.25))
                                                .tracking(1)
                                        }
                                    }

                                    // Active indicator
                                    Rectangle()
                                        .fill(selectedTab == server.id ? server.color : Color.clear)
                                        .frame(height: 2)
                                        .animation(.easeInOut(duration: 0.2), value: selectedTab)

                                    // Status dot
                                    Circle()
                                        .fill(server.online ? Color(red: 0.06, green: 0.73, blue: 0.51) : Color(white: 0.2))
                                        .frame(width: 4, height: 4)
                                }
                                .frame(maxWidth: .infinity)
                                .padding(.top, 10)
                                .padding(.bottom, 4)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .background(Color(red: 0.027, green: 0.047, blue: 0.086))
                    .overlay(
                        Rectangle().fill(Color(white: 0.05)).frame(height: 1),
                        alignment: .top
                    )
                    .padding(.bottom, safeAreaBottom())
                }
                .ignoresSafeArea(.container, edges: .bottom)
            }
        }
    }

    func safeAreaBottom() -> CGFloat {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .first?.windows.first?.safeAreaInsets.bottom ?? 0
    }
}

// MARK: - Discovery

struct DiscoveryView: View {
    @Binding var servers: [Server]
    @Binding var discovering: Bool
    @Binding var selectedTab: String

    var body: some View {
        VStack(spacing: 20) {
            Text("DISCOVERING")
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .foregroundColor(Color(white: 0.3))
                .tracking(4)

            ProgressView()
                .progressViewStyle(CircularProgressViewStyle(tint: Color(white: 0.3)))

            ForEach(servers) { s in
                HStack(spacing: 10) {
                    Circle()
                        .fill(s.online ? Color(red: 0.06, green: 0.73, blue: 0.51) : Color(white: 0.15))
                        .frame(width: 6, height: 6)
                    Text(s.name)
                        .font(.system(size: 11, weight: .bold, design: .monospaced))
                        .foregroundColor(s.online ? .white : Color(white: 0.3))
                    Spacer()
                    Text(s.online ? "ONLINE" : "SCANNING")
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .foregroundColor(s.online ? s.color : Color(white: 0.2))
                        .tracking(2)
                }
                .padding(.horizontal, 40)
            }
        }
        .onAppear { discover() }
    }

    func discover() {
        DispatchQueue.global(qos: .userInitiated).async {
            for i in servers.indices {
                for url in servers[i].urls {
                    if let u = URL(string: url) {
                        var req = URLRequest(url: u, timeoutInterval: 4)
                        req.httpMethod = "HEAD"
                        let sem = DispatchSemaphore(value: 0)
                        var ok = false
                        URLSession.shared.dataTask(with: req) { _, resp, _ in
                            if let http = resp as? HTTPURLResponse, http.statusCode < 500 { ok = true }
                            sem.signal()
                        }.resume()
                        sem.wait()
                        if ok {
                            DispatchQueue.main.async {
                                servers[i].resolvedURL = url
                                servers[i].online = true
                            }
                            break
                        }
                    }
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                // Default to first online server
                if let first = servers.first(where: { $0.online }) {
                    selectedTab = first.id
                }
                discovering = false
            }
        }
    }
}

// MARK: - WebView

struct NASWebView: UIViewRepresentable {
    let urlString: String

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []

        // Allow file uploads via camera/photos
        let prefs = WKWebpagePreferences()
        prefs.allowsContentJavaScript = true
        config.defaultWebpagePreferences = prefs

        let wv = WKWebView(frame: .zero, configuration: config)
        wv.isOpaque = false
        wv.backgroundColor = UIColor(red: 0.016, green: 0.027, blue: 0.051, alpha: 1)
        wv.scrollView.backgroundColor = wv.backgroundColor
        wv.allowsBackForwardNavigationGestures = true
        wv.scrollView.contentInsetAdjustmentBehavior = .never

        // Inject CSS to handle safe areas
        let css = """
        body { -webkit-touch-callout: default; }
        """
        let js = "var s=document.createElement('style');s.textContent=`\(css)`;document.head.appendChild(s);"
        let script = WKUserScript(source: js, injectionTime: .atDocumentEnd, forMainFrameOnly: true)
        wv.configuration.userContentController.addUserScript(script)

        if let url = URL(string: urlString) {
            wv.load(URLRequest(url: url))
        }
        return wv
    }

    func updateUIView(_ wv: WKWebView, context: Context) {}
}
