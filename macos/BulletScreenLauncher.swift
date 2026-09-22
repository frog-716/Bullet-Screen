import Cocoa

private final class ActiveRun {
    let identity: LaunchRunIdentity
    let provider: String
    let process: Process
    let stdout: Pipe
    let stderr: Pipe
    var port: Int?
    var outputBuffer = ""
    var errorBuffer = ""
    var readinessTask: URLSessionDataTask?
    var readinessDeadline: Date?
    var stopTimeoutWorkItem: DispatchWorkItem?
    var stopTimedOut = false
    var pendingStopMessage: String?
    var pageOpened = false

    init(identity: LaunchRunIdentity, provider: String, process: Process, stdout: Pipe, stderr: Pipe) {
        self.identity = identity
        self.provider = provider
        self.process = process
        self.stdout = stdout
        self.stderr = stderr
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private let providerControl = NSSegmentedControl(labels: ["Bilibili", "抖音"], trackingMode: .selectOne, target: nil, action: nil)
    private let demoControl = NSButton(checkboxWithTitle: "抖音使用本地演示模式", target: nil, action: nil)
    private let startButton = NSButton(title: "启动看板", target: nil, action: nil)
    private let stopButton = NSButton(title: "停止服务", target: nil, action: nil)
    private let statusLabel = NSTextField(wrappingLabelWithString: "选择平台后启动看板。")
    private let maxOutputBufferCharacters = 64 * 1024
    private var projectRoot: URL?
    private let lifecycle = LauncherLifecycleModel()
    private var activeRun: ActiveRun?
    private var preflightInProgress = false
    private var applicationTerminationRequested = false
    private let readyTimeout: TimeInterval = 10
    private let readyPollInterval: TimeInterval = 0.25
    private let stopTimeout: TimeInterval = 5

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        projectRoot = resolveProjectRoot()
        if let projectRoot {
            setStatus("已绑定项目：\(projectRoot.lastPathComponent)")
        } else {
            setStatus("请选择 Bullet-Screen 项目目录后再启动看板。", error: true)
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard activeRun != nil else { return .terminateNow }
        applicationTerminationRequested = true
        stopServer(updateUI: false)
        return activeRun == nil ? .terminateNow : .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        activeRun?.stdout.fileHandleForReading.readabilityHandler = nil
        activeRun?.stderr.fileHandleForReading.readabilityHandler = nil
    }

    private func buildWindow() {
        let content = NSView()
        content.translatesAutoresizingMaskIntoConstraints = false

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 390),
                          styleMask: [.titled, .closable, .miniaturizable],
                          backing: .buffered,
                          defer: false)
        window.title = "Bullet-Screen"
        window.isReleasedWhenClosed = false
        window.contentView = content
        window.center()

        let title = NSTextField(labelWithString: "直播弹幕看板")
        title.font = NSFont.systemFont(ofSize: 30, weight: .bold)

        let subtitle = NSTextField(wrappingLabelWithString: "选择平台后，Bullet-Screen 会启动对应的本地采集服务，并打开看板页面。")
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
        guard !preflightInProgress else { return }
        guard lifecycle.state == .stopped || lifecycle.state == .failed else {
            if lifecycle.state == .stopping {
                setStatus("服务仍在停止，确认进程退出后才能再次启动。", error: true)
            } else {
                setStatus("服务正在启动，请稍候。")
            }
            return
        }

        preflightInProgress = true
        startButton.isEnabled = false
        stopButton.isEnabled = false
        setStatus("正在检查运行环境…")

        if projectRoot == nil {
            projectRoot = chooseProjectRoot()
        }
        guard let root = projectRoot else {
            failPreflight("尚未选择有效的 Bullet-Screen 项目目录。")
            return
        }

        let provider = providerControl.selectedSegment == 1 ? "douyin" : "bilibili"
        let providerRoot = root.appendingPathComponent(provider, isDirectory: true)
        guard LauncherPreflight.hasServer(root: root.path, provider: provider) else {
            failPreflight("找不到 \(provider)/server.py。请确认项目文件完整。")
            return
        }

        guard let python = findPython(in: root) else {
            failPreflight("找不到可执行的 Python 3。请安装 Python 3，或设置 BULLET_SCREEN_PYTHON。")
            return
        }

        guard let port = LauncherPreflight.parsePort(ProcessInfo.processInfo.environment["BULLET_SCREEN_PORT"]) else {
            failPreflight("BULLET_SCREEN_PORT 不是有效端口；请使用 0 或 1-65535。")
            return
        }
        guard LauncherPreflight.isPortAvailable(port) else {
            failPreflight("端口 \(port) 已被占用，请关闭占用程序或设置其他 BULLET_SCREEN_PORT。")
            return
        }
        if let pythonError = pythonRuntimeError(python) {
            failPreflight(pythonError)
            return
        }
        if provider == "douyin" && demoControl.state != .on,
           let error = douyinRuntimeError(python) {
            failPreflight(error)
            return
        }

        let identity = lifecycle.beginStart()!
        let process = Process()
        process.executableURL = python
        var arguments = ["-u", "server.py", "--port", String(port)]
        if provider == "douyin" {
            arguments += ["--mode", demoControl.state == .on ? "demo" : "auto"]
        }
        if !(provider == "douyin" && demoControl.state == .on),
           let configuredDatabase = ProcessInfo.processInfo.environment["BULLET_SCREEN_DB"],
           !configuredDatabase.isEmpty {
            let databaseURL = URL(fileURLWithPath: configuredDatabase).standardizedFileURL
            guard LauncherPreflight.hasDatabaseParent(databaseURL.path) else {
                _ = lifecycle.fail(identity)
                failPreflight("BULLET_SCREEN_DB 的父目录不存在：\(databaseURL.deletingLastPathComponent().path)")
                return
            }
            arguments += ["--db", databaseURL.path]
        }
        process.arguments = arguments
        process.currentDirectoryURL = providerRoot

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        let run = ActiveRun(identity: identity, provider: provider, process: process, stdout: stdout, stderr: stderr)
        activeRun = run
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.handleTermination(run)
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
                self?.consumeServerOutput(text, run: run)
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
                self?.consumeServerError(text, run: run)
            }
        }

        do {
            try process.run()
            preflightInProgress = false
            stopButton.isEnabled = true
            setStatus("正在启动 \(provider == "douyin" ? "抖音" : "Bilibili") 服务，等待 ready…")
        } catch {
            handleLaunchFailure(run, message: "进程启动失败：\(error.localizedDescription)")
        }
    }

    @objc private func stopSelectedProvider() {
        stopServer(updateUI: true)
    }

    private func stopServer(updateUI: Bool) {
        guard let run = activeRun else {
            if updateUI { setStatus("本地服务已停止。") }
            updateControls()
            return
        }
        guard lifecycle.beginStop(for: run.identity) else {
            if lifecycle.state == .stopping, updateUI {
                setStatus("正在停止服务，请等待进程真正退出。", error: true)
            }
            return
        }
        run.readinessTask?.cancel()
        run.readinessTask = nil
        run.stdout.fileHandleForReading.readabilityHandler = nil
        run.stderr.fileHandleForReading.readabilityHandler = nil
        if run.process.isRunning {
            run.process.terminate()
            scheduleStopTimeout(run)
            setStatus(updateUI ? "正在停止服务，等待进程退出…" : statusLabel.stringValue)
        } else {
            handleTermination(run)
        }
        updateControls()
    }

    private func consumeServerOutput(_ text: String, run: ActiveRun) {
        guard activeRun?.identity == run.identity, lifecycle.accepts(run.identity) else { return }
        run.outputBuffer += text
        if run.outputBuffer.count > maxOutputBufferCharacters {
            run.outputBuffer = String(run.outputBuffer.suffix(maxOutputBufferCharacters))
        }
        while let newline = run.outputBuffer.firstIndex(of: "\n") {
            let line = String(run.outputBuffer[..<newline]).trimmingCharacters(in: .whitespacesAndNewlines)
            run.outputBuffer = String(run.outputBuffer[run.outputBuffer.index(after: newline)...])
            guard let port = extractPort(from: line) else { continue }
            if run.port == nil {
                run.port = port
                beginReadinessChecks(run)
            }
        }
    }

    private func consumeServerError(_ text: String, run: ActiveRun) {
        guard activeRun?.identity == run.identity, lifecycle.accepts(run.identity) else { return }
        run.errorBuffer += text
        if run.errorBuffer.count > 4 * 1024 {
            run.errorBuffer = String(run.errorBuffer.suffix(4 * 1024))
        }
        if run.port != nil { return }
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { return }
        setStatus("服务启动提示：\(String(message.suffix(220)))", error: true)
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
        panel.title = "选择 Bullet-Screen 项目目录"
        panel.message = "请选择包含 bilibili 和 douyin 子目录的 Bullet-Screen 文件夹。"
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
        return candidates.first { LauncherPreflight.isExecutable($0) }.map(URL.init(fileURLWithPath:))
    }

    private func failPreflight(_ message: String) {
        preflightInProgress = false
        activeRun = nil
        updateControls()
        setStatus("无法启动：\(message)", error: true)
    }

    private func updateControls() {
        startButton.isEnabled = !preflightInProgress && (lifecycle.state == .stopped || lifecycle.state == .failed)
        stopButton.isEnabled = lifecycle.state == .starting || lifecycle.state == .waitingForReady || lifecycle.state == .running
    }

    private func runPythonCheck(_ python: URL, script: String) -> (Bool, String) {
        let result = LauncherPreflight.runPythonCheck(at: python.path, script: script)
        return (result.ok, result.output)
    }

    private func pythonRuntimeError(_ python: URL) -> String? {
        let result = runPythonCheck(python, script: "import sys; print(sys.version.split()[0])")
        return result.0 ? nil : "Python 无法运行：\(result.1)"
    }

    private func douyinRuntimeError(_ python: URL) -> String? {
        let script = "import os; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); path=p.chromium.executable_path; p.stop(); assert os.path.isfile(path), path; print(path)"
        let result = runPythonCheck(python, script: script)
        return result.0 ? nil : "抖音真实模式缺少可用的 Playwright/Chromium：\(result.1)"
    }

    private func beginReadinessChecks(_ run: ActiveRun) {
        guard activeRun?.identity == run.identity, lifecycle.accepts(run.identity), let port = run.port else { return }
        _ = lifecycle.markWaitingForReady(run.identity)
        run.readinessDeadline = Date().addingTimeInterval(readyTimeout)
        setStatus("服务已监听端口 \(port)，正在等待 health/bootstrap ready…")
        pollReadiness(run)
    }

    private func pollReadiness(_ run: ActiveRun) {
        guard activeRun?.identity == run.identity, lifecycle.accepts(run.identity), let port = run.port else { return }
        guard let deadline = run.readinessDeadline else { return }
        guard Date() < deadline else {
            handleLaunchFailure(run, message: "服务没有在规定时间内 ready。")
            return
        }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/api/health")!)
        request.timeoutInterval = 1
        run.readinessTask = URLSession.shared.dataTask(with: request) { [weak self, weak run] data, response, _ in
            guard let self, let run else { return }
            let healthy = (response as? HTTPURLResponse)?.statusCode == 200
                && ((try? JSONSerialization.jsonObject(with: data ?? Data())) as? [String: Any])?["ok"] as? Bool == true
            DispatchQueue.main.async {
                guard self.activeRun?.identity == run.identity, self.lifecycle.accepts(run.identity) else { return }
                if healthy {
                    self.checkBootstrap(run)
                } else {
                    self.scheduleReadinessPoll(run)
                }
            }
        }
        run.readinessTask?.resume()
    }

    private func checkBootstrap(_ run: ActiveRun) {
        guard activeRun?.identity == run.identity, lifecycle.accepts(run.identity), let port = run.port else { return }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/api/bootstrap")!)
        request.timeoutInterval = 1
        run.readinessTask = URLSession.shared.dataTask(with: request) { [weak self, weak run] data, response, _ in
            guard let self, let run else { return }
            let payload = (try? JSONSerialization.jsonObject(with: data ?? Data())) as? [String: Any]
            let ready = (response as? HTTPURLResponse)?.statusCode == 200
                && (payload?["token"] as? String)?.isEmpty == false
            DispatchQueue.main.async {
                guard self.activeRun?.identity == run.identity, self.lifecycle.accepts(run.identity) else { return }
                if ready {
                    run.readinessTask = nil
                    _ = self.lifecycle.markReady(run.identity)
                    self.updateControls()
                    self.setStatus("\(run.provider == "douyin" ? "抖音" : "Bilibili") 服务已 ready · 端口 \(port)")
                    if !run.pageOpened {
                        run.pageOpened = true
                        NSWorkspace.shared.open(URL(string: "http://127.0.0.1:\(port)/")!)
                    }
                } else {
                    self.scheduleReadinessPoll(run)
                }
            }
        }
        run.readinessTask?.resume()
    }

    private func scheduleReadinessPoll(_ run: ActiveRun) {
        run.readinessTask = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + readyPollInterval) { [weak self, weak run] in
            guard let self, let run else { return }
            self.pollReadiness(run)
        }
    }

    private func scheduleStopTimeout(_ run: ActiveRun) {
        let item = DispatchWorkItem { [weak self, weak run] in
            guard let self, let run,
                  self.activeRun?.identity == run.identity,
                  self.lifecycle.state == .stopping,
                  run.process.isRunning else { return }
            _ = self.lifecycle.markStopTimedOut(for: run.identity)
            run.stopTimedOut = true
            run.pendingStopMessage = "停止请求超时；服务进程仍在运行，尚未确认 stopped。"
            self.setStatus(run.pendingStopMessage!, error: true)
            self.updateControls()
        }
        run.stopTimeoutWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + stopTimeout, execute: item)
    }

    private func handleLaunchFailure(_ run: ActiveRun, message: String) {
        guard activeRun?.identity == run.identity else { return }
        preflightInProgress = false
        run.pendingStopMessage = "启动失败：\(message)"
        if lifecycle.beginStop(for: run.identity) {
            run.stdout.fileHandleForReading.readabilityHandler = nil
            run.stderr.fileHandleForReading.readabilityHandler = nil
            if run.process.isRunning {
                run.process.terminate()
                scheduleStopTimeout(run)
                setStatus("\(message) 正在停止服务…", error: true)
            } else {
                handleTermination(run)
            }
            updateControls()
        } else {
            _ = lifecycle.fail(run.identity)
            cleanupRun(run)
            setStatus(run.pendingStopMessage!, error: true)
            updateControls()
        }
    }

    private func handleTermination(_ run: ActiveRun) {
        guard activeRun?.identity == run.identity else { return }
        run.stopTimeoutWorkItem?.cancel()
        run.readinessTask?.cancel()
        run.readinessTask = nil
        let wasStopping = lifecycle.state == .stopping
        let exitCode = run.process.terminationStatus
        if wasStopping {
            _ = lifecycle.finishStop(for: run.identity)
            let message = run.pendingStopMessage ?? (run.stopTimedOut ? "本地服务已退出（此前停止请求曾超时）。" : "本地服务已停止。")
            cleanupRun(run)
            setStatus(message)
        } else {
            _ = lifecycle.fail(run.identity)
            let detail = run.errorBuffer.trimmingCharacters(in: .whitespacesAndNewlines)
            let phase = run.port == nil ? "服务在 ready 前退出" : "服务进程已退出"
            let suffix = detail.isEmpty ? "" : "：\(String(detail.suffix(180)))"
            cleanupRun(run)
            setStatus("\(phase)（退出码 \(exitCode)）\(suffix)", error: true)
        }
        preflightInProgress = false
        updateControls()
        if applicationTerminationRequested {
            applicationTerminationRequested = false
            NSApp.reply(toApplicationShouldTerminate: true)
        }
    }

    private func cleanupRun(_ run: ActiveRun) {
        guard activeRun?.identity == run.identity else { return }
        run.stdout.fileHandleForReading.readabilityHandler = nil
        run.stderr.fileHandleForReading.readabilityHandler = nil
        activeRun = nil
    }
}

@main
struct BulletScreenLauncherMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.run()
    }
}
