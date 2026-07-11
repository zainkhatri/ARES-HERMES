import SwiftUI

@main
struct PROMETHEONApp: App {
    @State private var showLaunch = true

    var body: some Scene {
        WindowGroup {
            ZStack {
                ContentView()
                    .ignoresSafeArea()

                if showLaunch {
                    LaunchScreen()
                        .transition(.opacity)
                        .zIndex(1)
                }
            }
            .onAppear {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                    withAnimation(.easeOut(duration: 0.5)) {
                        showLaunch = false
                    }
                }
            }
        }
    }
}

struct LaunchScreen: View {
    @State private var barWidth: CGFloat = 0
    @State private var glowOpacity: Double = 0.3

    var body: some View {
        ZStack {
            Color(red: 0.016, green: 0.027, blue: 0.051)
                .ignoresSafeArea()

            VStack(spacing: 20) {
                BlockP(size: 90)
                    .shadow(color: Color(red: 0.494, green: 0.722, blue: 0.941).opacity(glowOpacity), radius: 20)

                Text("PROMETHEON")
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .tracking(8)
                    .foregroundColor(Color(red: 0.494, green: 0.722, blue: 0.941).opacity(0.4))

                // Loading bar
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Color.white.opacity(0.05))
                        .frame(width: 200, height: 3)

                    RoundedRectangle(cornerRadius: 2)
                        .fill(Color(red: 0.494, green: 0.722, blue: 0.941))
                        .frame(width: barWidth, height: 3)
                        .shadow(color: Color(red: 0.494, green: 0.722, blue: 0.941).opacity(0.6), radius: 6)
                }
                .padding(.top, 16)
            }
        }
        .onAppear {
            withAnimation(.easeInOut(duration: 1.8)) {
                barWidth = 200
            }
            withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true)) {
                glowOpacity = 0.8
            }
        }
    }
}

/// Block-character style "P" built from rectangles
struct BlockP: View {
    let size: CGFloat
    private let accent = Color(red: 0.494, green: 0.722, blue: 0.941)

    var body: some View {
        let u = size / 7
        let r: CGFloat = size / 30
        ZStack(alignment: .topLeading) {
            // Vertical stem
            RoundedRectangle(cornerRadius: r)
                .fill(accent)
                .frame(width: u * 2, height: u * 7)
            // Top bar
            RoundedRectangle(cornerRadius: r)
                .fill(accent)
                .frame(width: u * 5.5, height: u * 1.4)
            // Right arm
            RoundedRectangle(cornerRadius: r)
                .fill(accent)
                .frame(width: u * 1.7, height: u * 3.8)
                .offset(x: u * 3.8)
            // Middle bar
            RoundedRectangle(cornerRadius: r)
                .fill(accent)
                .frame(width: u * 5.5, height: u * 1.2)
                .offset(y: u * 2.8)
        }
        .frame(width: size * 0.79, height: size)
    }
}
