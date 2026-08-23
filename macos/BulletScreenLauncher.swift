import Cocoa

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private let providerControl = NSSegmentedControl(labels: ["Bilibili", "抖音"], trackingMode: .selectOne, target: nil, action: nil)
    private let demoControl = NSButton(checkboxWithTitle: "抖音使用本地演示模式", target: nil, action: nil)
    private let startButton = NSButton(title: "启动看板", target: nil, action: nil)
    private let stopButton = NSButton(title: "停止服务", target: nil, action: nil)
    private let statusLabel = NSTextField(wrappingLabelWithString: "选择平台后启动看板。")
    private var serverProcess: Process?
    private var outputPipe: Pipe?
    private var errorPipe: Pipe?
    private var outputBuffer = ""
    private var currentPort: Int?
    private var projectRoot: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        projectRoot = resolveProjectRoot()
        if let projectRoot {
            setStatus("已绑定项目：\(projectRoot.lastPathComponent)")
        } else {
            setStatus("请选择 bullet-screen 项目目录后再启动看板。", error: true)
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopServer(updateUI: false)
    }

    private func buildWindow() {
        let content = NSView()
        content.translatesAutoresizingMaskIntoConstraints = false

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 390),
                          styleMask: [.titled, .closable, .miniaturizable],
                          backing: .buffered,
                          defer: false)
        window.title = "bullet-screen"
        window.isReleasedWhenClosed = false
        window.contentView = content
        window.center()

        let title = NSTextField(labelWithString: "直播弹幕看板")
        title.font = NSFont.systemFont(ofSize: 30, weight: .bold)

        let subtitle = NSTextField(wrappingLabelWithString: "选择平台后，bullet-screen 会启动对应的本地采集服务，并打开看板页面。")
        subtitle.textColor = .secondaryLabelColor
        subtitle.font = NSFont.systemFont(ofSize: 14)

        let providerLabel = NSTextField(labelWithString: "连接平台")
        providerLabel.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        providerControl.selectedSegment = 0
        providerControl.segmentStyle = .rounded
        providerControl.target = self
        providerControl.action = #selector(providerChanged)

        demoControl.state = .off
        demoControl.font = NSFont.systemFont(ofSize: 12)
        demoControl.target = self
        demoControl.action = #selector(providerChanged)

        startButton.keyEquivalent = "\r"
        startButton.bezelStyle = .rounded
        startButton.target = self
        startButton.action = #selector(startSelectedProvider)

        stopButton.bezelStyle = .rounded
        stopButton.target = self
        stopButton.action = #selector(stopSelectedProvider)
        stopButton.isEnabled = false

        statusLabel.textColor = .secondaryLabelColor
        statusLabel.maximumNumberOfLines = 3
        statusLabel.preferredMaxLayoutWidth = 500

        let actionRow = NSStackView(views: [startButton, stopButton])
        actionRow.orientation = .horizontal
        actionRow.spacing = 10

        let stack = NSStackView(views: [title, subtitle, providerLabel, providerControl, demoControl, actionRow, statusLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 34),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -34),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 32),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -28),
            providerControl.widthAnchor.constraint(equalToConstant: 230),
            providerControl.heightAnchor.constraint(equalToConstant: 30),
            statusLabel.widthAnchor.constraint(equalToConstant: 490)
        ])
        providerChanged()
    }

    @objc private func providerChanged() {
        let isDouyin = providerControl.selectedSegment == 1
        demoControl.isHidden = !isDouyin
        startButton.title = isDouyin ? "启动抖音看板" : "启动 Bilibili 看板"
    }

    @objc private func startSelectedProvider() {
        if let process = serverProcess, process.isRunning {
            stopServer(updateUI: false)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
                self?.startSelectedProvider()
            }
            return
        }

        if projectRoot == nil {
            projectRoot = chooseProjectRoot()
        }
        guard let root = projectRoot else {
            setStatus("尚未选择有效的 bullet-screen 项目目录。请再次点击启动后选择。", error: true)
            return
        }

        let provider = providerControl.selectedSegment == 1 ? "douyin" : "bilibili"
        let providerRoot = root.appendingPathComponent(provider, isDirectory: true)
        let serverFile = providerRoot.appendingPathComponent("server.py")
        guard FileManager.default.fileExists(atPath: serverFile.path) else {
            setStatus("找不到 \(provider)/server.py。请确认项目文件完整。", error: true)
            return
        }

        guard let python = findPython(in: root) else {
            setStatus("找不到 Python 3。请安装 Python 3，或设置 BULLET_SCREEN_PYTHON。", error: true)
            return
        }

        let process = Process()
        process.executableURL = python
        var arguments = ["-u", "server.py", "--port", "0"]
        if provider == "douyin" {
            arguments += ["--mode", demoControl.state == .on ? "demo" : "auto"]
        }
        process.arguments = arguments
        process.currentDirectoryURL = providerRoot

        let stdout = Pipe()
        let stderr = Pipe()
        outputPipe = stdout
        errorPipe = stderr
        process.standardOutput = stdout
        process.standardError = stderr
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self else { return }
                self.serverProcess = nil
                self.outputPipe = nil
                self.errorPipe = nil
                self.currentPort = nil
                self.stopButton.isEnabled = false
                self.startButton.isEnabled = true
                if self.statusLabel.stringValue.contains("已启动") {
                    self.setStatus("本地服务已停止。")
                }
            }
        }

        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            let text = String(data: data, encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                self?.consumeServerOutput(text, provider: provider)
            }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            let text = String(data: data, encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                self?.consumeServerError(text)
            }
        }

        do {
            try process.run()
            serverProcess = process
            startButton.isEnabled = false
            stopButton.isEnabled = true
            setStatus("正在启动 \(provider == "douyin" ? "抖音" : "Bilibili") 服务…")
        } catch {
            setStatus("启动失败：\(error.localizedDescription)", error: true)
        }
    }

    @objc private func stopSelectedProvider() {
        stopServer(updateUI: true)
    }

    private func stopServer(updateUI: Bool) {
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        errorPipe?.fileHandleForReading.readabilityHandler = nil
        if let process = serverProcess, process.isRunning {
            process.terminate()
        }
        serverProcess = nil
        outputPipe = nil
        errorPipe = nil
        currentPort = nil
        if updateUI {
            stopButton.isEnabled = false
            startButton.isEnabled = true
            setStatus("本地服务已停止。")
        }
    }

    private func consumeServerOutput(_ text: String, provider: String) {
        outputBuffer += text
        while let newline = outputBuffer.firstIndex(of: "\n") {
            let line = String(outputBuffer[..<newline]).trimmingCharacters(in: .whitespacesAndNewlines)
            outputBuffer = String(outputBuffer[outputBuffer.index(after: newline)...])
            guard let port = extractPort(from: line) else { continue }
            currentPort = port
            setStatus("已启动 \(provider == "douyin" ? "抖音" : "Bilibili") 看板 · 端口 \(port)")
            if let url = URL(string: "http://127.0.0.1:\(port)/") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    private func consumeServerError(_ text: String) {
        if currentPort != nil { return }
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { return }
        setStatus("服务启动提示：\(message.suffix(220))", error: true)
    }

    private func extractPort(from line: String) -> Int? {
        guard let marker = line.range(of: "http://127.0.0.1:") else { return nil }
        let remainder = line[marker.upperBound...]
        guard let slash = remainder.firstIndex(of: "/") else { return nil }
        return Int(remainder[..<slash])
    }

    private func setStatus(_ text: String, error: Bool = false) {
        statusLabel.stringValue = text
        statusLabel.textColor = error ? .systemRed : .secondaryLabelColor
    }

    private func resolveProjectRoot() -> URL? {
        var candidates: [URL] = []
        if let configured = ProcessInfo.processInfo.environment["BULLET_SCREEN_ROOT"], !configured.isEmpty {
            candidates.append(URL(fileURLWithPath: configured))
        }
        if let stored = UserDefaults.standard.string(forKey: "projectRoot"), !stored.isEmpty {
            candidates.append(URL(fileURLWithPath: stored))
        }
        candidates.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath))
        candidates.append(URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().deletingLastPathComponent())

        for candidate in candidates {
            if let root = searchProjectAncestors(from: candidate) {
                rememberProjectRoot(root)
                return root
            }
        }

        return chooseProjectRoot()
    }

    private func chooseProjectRoot() -> URL? {
        let panel = NSOpenPanel()
        panel.title = "选择 bullet-screen 项目目录"
        panel.message = "请选择包含 bilibili 和 douyin 子目录的 bullet-screen 文件夹。"
        panel.prompt = "选择项目"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let selected = panel.url,
              let root = searchProjectAncestors(from: selected) else {
            return nil
        }
        rememberProjectRoot(root)
        return root
    }

    private func searchProjectAncestors(from url: URL) -> URL? {
        var candidate = url.resolvingSymlinksInPath().standardizedFileURL
        if !FileManager.default.fileExists(atPath: candidate.path, isDirectory: nil) {
            candidate.deleteLastPathComponent()
        }
        for _ in 0..<12 {
            if isProjectRoot(candidate) { return candidate }
            let parent = candidate.deletingLastPathComponent()
            if parent.path == candidate.path { break }
            candidate = parent
        }
        return nil
    }

    private func isProjectRoot(_ url: URL) -> Bool {
        let manager = FileManager.default
        return manager.fileExists(atPath: url.appendingPathComponent("bilibili/server.py").path)
            && manager.fileExists(atPath: url.appendingPathComponent("douyin/server.py").path)
    }

    private func rememberProjectRoot(_ url: URL) {
        UserDefaults.standard.set(url.standardizedFileURL.path, forKey: "projectRoot")
    }

    private func findPython(in root: URL) -> URL? {
        var candidates: [String] = []
        if let configured = ProcessInfo.processInfo.environment["BULLET_SCREEN_PYTHON"], !configured.isEmpty {
            candidates.append(configured)
        }
        candidates += [
            root.appendingPathComponent(".venv/bin/python3").path,
            root.appendingPathComponent(".venv/bin/python").path,
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3"
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }.map(URL.init(fileURLWithPath:))
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.run()
