# A&H Offline Photos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the iOS app from PROMETHEON to A&H (strings, icon component, plist) — the offline photo caching infrastructure is already fully deployed.

**Architecture:** The service worker (`static/sw.js`) already caches the app shell and handles offline HTML navigation. Caddy and Flask already send `Cache-Control: public, max-age=604800, immutable` on all thumb tiers. WKWebView's native HTTP cache handles per-photo offline caching automatically. The only remaining work is a cosmetic rename of the iOS app.

**Tech Stack:** SwiftUI, WKWebView, Xcode project (`ios/PROMETHEON/`), Info.plist

## Global Constraints

- Target: the Xcode project at `ios/PROMETHEON/` — do NOT rename the directory (Xcode project refs will break)
- No new dependencies
- Keep the same dark navy color `Color(red: 0.024, green: 0.039, blue: 0.071)` and blue accent `Color(red: 0.494, green: 0.722, blue: 0.941)` — brand colors unchanged
- `BlockAH` must render cleanly at both 80pt (setup screen) and 90pt (launch screen)
- No tests needed: all changes are visual string/shape renames with no logic

---

## Context: What's Already Done

Before starting, note that the following spec items are **already implemented** and require no changes:

| Item | Status | Evidence |
|---|---|---|
| `static/sw.js` with app-shell offline caching | Done | File exists, registered in `photos.html` at line 8480 |
| `/sw.js` Flask route with `Service-Worker-Allowed: /` | Done | `app.py:4551-4558` |
| `Cache-Control: public, max-age=604800, immutable` on Caddy thumbs | Done | `Caddyfile.proposed` static_media snippet |
| `Cache-Control: public, max-age=604800, immutable` on Flask cold-path thumbs | Done | `app.py:237, 276` |
| App icon PNGs regenerated to A&H pixel art | Done | Committed in previous session |

---

## File Changelist

| File | Change |
|---|---|
| `ios/PROMETHEON/App.swift` | Rename `PROMETHEONApp` → `AHApp`; rename `BlockP` → `BlockAH` with new shape; update `Text("PROMETHEON")` → `Text("A&H")` |
| `ios/PROMETHEON/ContentView.swift` | Update `Text("PROMETHEON")` → `Text("A&H")`; update `BlockP(...)` → `BlockAH(...)` calls |
| `ios/PROMETHEON/Info.plist` | `CFBundleDisplayName` → `A&H`; update both `NSPhotoLibrary*UsageDescription` strings |

---

### Task 1: Rename App.swift — struct, strings, BlockP → BlockAH

**Files:**
- Modify: `ios/PROMETHEON/App.swift`

**No tests** — visual rename, no logic. Verify by inspection.

- [ ] **Step 1: Rewrite `App.swift`**

Replace the full file contents with:

```swift
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
```

- [ ] **Step 2: Verify the file compiles (Xcode or `swiftc` dry-run)**

Open the project in Xcode and confirm no red errors on `App.swift`. The struct name `AHApp` must match the `@main` entry point — Xcode will flag it immediately if not.

- [ ] **Step 3: Commit**

```bash
git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  add ios/PROMETHEON/App.swift && \
  git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  commit -m "Rename app PROMETHEON→A&H: AHApp struct, BlockAH component, launch screen strings"
```

---

### Task 2: Update ContentView.swift strings and BlockAH calls

**Files:**
- Modify: `ios/PROMETHEON/ContentView.swift`

- [ ] **Step 1: Replace `Text("PROMETHEON")` with `Text("A&H")` in SetupView**

In `ContentView.swift`, `SetupView.body` contains:

```swift
// OLD — find this:
Text("PROMETHEON")
    .font(.system(size: 14, weight: .semibold, design: .monospaced))
    .tracking(6)
    .foregroundColor(Color(red: 0.494, green: 0.722, blue: 0.941).opacity(0.5))
```

Replace with:

```swift
Text("A&H")
    .font(.system(size: 14, weight: .semibold, design: .monospaced))
    .tracking(6)
    .foregroundColor(Color(red: 0.494, green: 0.722, blue: 0.941).opacity(0.5))
```

- [ ] **Step 2: Replace `BlockP(size: 80)` with `BlockAH(size: 80)` in SetupView**

In `SetupView.body`, find:

```swift
BlockP(size: 80)
```

Replace with:

```swift
BlockAH(size: 80)
```

- [ ] **Step 3: Delete the `BlockP` struct from `ContentView.swift`**

The `BlockP` struct definition starts at the bottom of `ContentView.swift` (around line 74 of `App.swift` — it may be in `App.swift`, not `ContentView.swift`). Confirm it's been moved/replaced by `BlockAH` in Task 1, then delete any remaining `BlockP` definition from `ContentView.swift` if present. If `BlockP` is only defined in `App.swift`, no action needed here.

- [ ] **Step 4: Commit**

```bash
git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  add ios/PROMETHEON/ContentView.swift && \
  git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  commit -m "ContentView: A&H strings + BlockAH refs"
```

---

### Task 3: Update Info.plist display name and usage strings

**Files:**
- Modify: `ios/PROMETHEON/Info.plist`

- [ ] **Step 1: Update `CFBundleDisplayName`**

Find:
```xml
<key>CFBundleDisplayName</key>
<string>PROMETHEON</string>
```

Replace with:
```xml
<key>CFBundleDisplayName</key>
<string>A&amp;H</string>
```

Note: XML requires `&amp;` for the `&` character in plist strings.

- [ ] **Step 2: Update `NSPhotoLibraryAddUsageDescription`**

Find:
```xml
<key>NSPhotoLibraryAddUsageDescription</key>
<string>PROMETHEON needs access to save downloaded photos to your Camera Roll.</string>
```

Replace with:
```xml
<key>NSPhotoLibraryAddUsageDescription</key>
<string>A&amp;H needs access to save downloaded photos to your Camera Roll.</string>
```

- [ ] **Step 3: Update `NSPhotoLibraryUsageDescription`**

Find:
```xml
<key>NSPhotoLibraryUsageDescription</key>
<string>PROMETHEON needs access to upload photos to the photosNAS.</string>
```

Replace with:
```xml
<key>NSPhotoLibraryUsageDescription</key>
<string>A&amp;H needs access to upload photos to the homelab.</string>
```

- [ ] **Step 4: Commit**

```bash
git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  add ios/PROMETHEON/Info.plist && \
  git -C /mnt/nvme/PROMETHEUS/PROJECTS/ARES-DASHBOARD \
  commit -m "Info.plist: display name and usage strings → A&H"
```

---

### Task 4: Manual offline verification

No code changes — confirm the existing offline stack works end-to-end on device.

- [ ] **Step 1: Install the updated app on device**

Build and run from Xcode. Confirm launch screen shows "A&H" text and the new BlockAH shape.

- [ ] **Step 2: Browse photos while on tailnet**

Open the photos tab. Scroll through a few months so thumbnails load. This warms the WKWebView HTTP cache and the service worker page cache.

- [ ] **Step 3: Go offline (airplane mode) and relaunch**

Enable airplane mode. Kill and relaunch the A&H app. Expected:
- Launch screen appears normally
- App loads the photos page (SW serves cached HTML)
- Previously viewed thumbnails appear (WKWebView HTTP cache)
- Unviewed thumbnails show blank/broken-image (expected — not cached yet)
- The app does NOT drop to the setup screen

- [ ] **Step 4: Confirm setup screen does not appear prematurely**

If the setup screen appears while offline, the `didFailProvisionalNavigation` guard in `ContentView.swift` (lines 311-315) is triggering before the SW can intercept. This would indicate the SW hasn't claimed the client yet — force-close, go back online briefly, reopen, then go offline again. On second offline attempt the SW will be active.

This is a known first-launch edge case, not a bug. No code fix needed unless it regresses after SW is active.
