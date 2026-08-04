import SwiftUI

@main
struct AHApp: App {
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
                BlockAH(size: 90)
                    .shadow(color: Color(red: 0.494, green: 0.722, blue: 0.941).opacity(glowOpacity), radius: 20)

                Text("A&H")
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .tracking(8)
                    .foregroundColor(Color(red: 0.494, green: 0.722, blue: 0.941).opacity(0.4))

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

/// Block-character "AH" — two letter forms built from rounded rectangles.
/// `size` is the cap height; total width is ~1.6× size to fit both letters.
struct BlockAH: View {
    let size: CGFloat
    private let accent = Color(red: 0.494, green: 0.722, blue: 0.941)

    var body: some View {
        let u = size / 7   // unit — same grid as the old BlockP
        let r: CGFloat = size / 30
        let gap: CGFloat = u * 1.2  // space between A and H

        ZStack(alignment: .topLeading) {
            // ── A ──
            // Left stem
            RoundedRectangle(cornerRadius: r).fill(accent)
                .frame(width: u * 1.6, height: u * 7)
            // Right stem
            RoundedRectangle(cornerRadius: r).fill(accent)
                .frame(width: u * 1.6, height: u * 7)
                .offset(x: u * 2.8)
            // Top bar (joins the stems)
            RoundedRectangle(cornerRadius: r).fill(accent)
                .frame(width: u * 4.4, height: u * 1.3)
            // Crossbar (mid-height)
            RoundedRectangle(cornerRadius: r).fill(accent)
                .frame(width: u * 4.4, height: u * 1.1)
                .offset(y: u * 2.9)

            // ── H (offset right by A-width + gap) ──
            let hx = u * 4.4 + gap
            // Left stem
            RoundedRectangle(cornerRadius: r).fill(accent)
                .frame(width: u * 1.6, height: u * 7)
                .offset(x: hx)
            // Right stem
            RoundedRectangle(cornerRadius: r).fill(accent)
                .frame(width: u * 1.6, height: u * 7)
                .offset(x: hx + u * 2.8)
            // Crossbar (mid-height)
            RoundedRectangle(cornerRadius: r).fill(accent)
                .frame(width: u * 4.4, height: u * 1.1)
                .offset(x: hx, y: u * 2.9)
        }
        .frame(width: u * 4.4 + gap + u * 4.4, height: size)
    }
}
