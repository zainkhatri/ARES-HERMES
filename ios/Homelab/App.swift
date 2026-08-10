import SwiftUI

@main
struct HomelabApp: App {
    @State private var showLaunch = true

    var body: some Scene {
        WindowGroup {
            ZStack {
                MainView()
                    .ignoresSafeArea()

                if showLaunch {
                    LaunchScreen()
                        .transition(.opacity)
                        .zIndex(1)
                }
            }
            .onAppear {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                    withAnimation(.easeOut(duration: 0.4)) {
                        showLaunch = false
                    }
                }
            }
        }
    }
}

// MARK: - Launch Screen

struct LaunchScreen: View {
    @State private var barWidth: CGFloat = 0

    var body: some View {
        ZStack {
            Color(red: 0.016, green: 0.027, blue: 0.051)
                .ignoresSafeArea()

            VStack(spacing: 28) {
                HStack(spacing: 24) {
                    BrandMark(letter: "N", color: Color(red: 0.39, green: 0.58, blue: 0.93))
                    BrandMark(letter: "A", color: Color(red: 0.94, green: 0.27, blue: 0.27))
                }

                VStack(spacing: 6) {
                    Text("HOMELAB")
                        .font(.system(size: 16, weight: .bold, design: .monospaced))
                        .foregroundColor(.white)
                        .tracking(8)

                    Text("HERMES · ARES")
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundColor(Color(white: 0.4))
                        .tracking(4)
                }

                // Loading bar
                ZStack(alignment: .leading) {
                    Rectangle()
                        .fill(Color(white: 0.08))
                        .frame(width: 120, height: 2)
                    Rectangle()
                        .fill(
                            LinearGradient(
                                colors: [
                                    Color(red: 0.39, green: 0.58, blue: 0.93),
                                    Color(red: 0.94, green: 0.27, blue: 0.27)
                                ],
                                startPoint: .leading,
                                endPoint: .trailing
                            )
                        )
                        .frame(width: barWidth, height: 2)
                }
                .onAppear {
                    withAnimation(.easeInOut(duration: 1.2)) {
                        barWidth = 120
                    }
                }
            }
        }
    }
}

struct BrandMark: View {
    let letter: String
    let color: Color

    var body: some View {
        Text(letter)
            .font(.system(size: 28, weight: .black, design: .default))
            .foregroundColor(color)
            .frame(width: 48, height: 48)
            .overlay(
                RoundedRectangle(cornerRadius: 0)
                    .stroke(color.opacity(0.6), lineWidth: 1.5)
            )
    }
}
