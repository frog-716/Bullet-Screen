import Cocoa

private final class ActiveRun {
    let identity: LaunchRunIdentity
    let provider: String
    let roomInput: String?
    let demoMode: Bool
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

    init(identity: LaunchRunIdentity, provider: String, roomInput: String?, demoMode: Bool, process: Process, stdout: Pipe, stderr: Pipe) {
        self.identity = identity
        self.provider = provider
        self.roomInput = roomInput
        self.demoMode = demoMode
        self.process = process
        self.stdout = stdout
        self.stderr = stderr
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private let providerControl = NSSegmentedControl(labels: ["Bilibili", "抖音"], trackingMode: .selectOne, target: nil, action: nil)
    private let roomInputField = NSTextField()
    private let roomHelpLabel = NSTextField(wrappingLabelWithString: "")
    private let demoButton = NSButton(title: "先试 Demo（不需要账号）", target: nil, action: nil)
    private let loginButton = NSButton(title: "登录 Douyin", target: nil, action: nil)
    private let doctorButton = NSButton(title: "检查安装环境", target: nil, action: nil)
    private let startButton = NSButton(title: "启动看板", target: nil, action: nil)
    private let stopButton = NSButton(title: "停止服务", target: nil, action: nil)
    private let statusLabel = NSTextField(wrappingLabelWithString: "选择平台后启动看板。")
    private let maxOutputBufferCharacters = 64 * 1024
    private var projectRoot: URL?
    private let lifecycle = LauncherLifecycleModel()
    private var activeRun: ActiveRun?
    private var loginProcess: Process?
    private var launchDemoRequested = false
    private var preflightInProgress = false
    private var applicationTerminationRequested = false
    private let readyTimeout: TimeInterval = 10
    private let readyPollInterval: TimeInterval = 0.25
    private let stopTimeout: TimeInterval = 5

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        projectRoot = resolveProjectRoot()
        if projectRoot != nil {
            setStatus("已找到项目。请选择平台，输入直播间链接或房间号；也可以先试 Demo。")
        } else {
            setStatus("请选择 Bullet-Screen 项目文件夹。", error: true)
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
        loginProcess?.terminate()
    }

    private func buildWindow() {
        let content = NSView()
        content.translatesAutoresizingMaskIntoConstraints = false

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 560),
                          styleMask: [.titled, .closable, .miniaturizable],
                          backing: .buffered,
                          defer: false)
        window.title = "Bullet-Screen"
        window.isReleasedWhenClosed = false
        window.contentView = content
        window.center()

        let title = NSTextField(labelWithString: "直播弹幕看板")
        title.font = NSFont.systemFont(ofSize: 30, weight: .bold)

        let subtitle = NSTextField(wrappingLabelWithString: "选择平台，填入直播间，点击开始。第一次使用也可以先试 Demo。")
        subtitle.textColor = .secondaryLabelColor
        subtitle.font = NSFont.systemFont(ofSize: 14)

        let providerLabel = NSTextField(labelWithString: "连接平台")
        providerLabel.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        providerControl.selectedSegment = 0
        providerControl.segmentStyle = .rounded
        providerControl.target = self
        providerControl.action = #selector(providerChanged)

        let platformHelp = NSTextField(wrappingLabelWithString: "Bilibili：可以直接尝试连接公开直播间。\nDouyin：需要额外的浏览器环境，部分房间可能只能打开页面，暂时读不到互动数据。")
        platformHelp.textColor = .secondaryLabelColor
        platformHelp.font = NSFont.systemFont(ofSize: 12)
        platformHelp.preferredMaxLayoutWidth = 540

        let roomLabel = NSTextField(labelWithString: "直播间链接或房间号")
        roomLabel.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        roomInputField.placeholderString = "例如：123456 或直播间链接"
        roomInputField.font = NSFont.systemFont(ofSize: 14)
        roomHelpLabel.textColor = .secondaryLabelColor
        roomHelpLabel.font = NSFont.systemFont(ofSize: 12)
        roomHelpLabel.preferredMaxLayoutWidth = 540

        startButton.keyEquivalent = "\r"
        startButton.bezelStyle = .rounded
        startButton.target = self
        startButton.action = #selector(startSelectedProvider)

        demoButton.bezelStyle = .rounded
        demoButton.target = self
        demoButton.action = #selector(startDemo)

        loginButton.bezelStyle = .rounded
        loginButton.target = self
        loginButton.action = #selector(loginDouyin)

        doctorButton.bezelStyle = .rounded
        doctorButton.target = self
        doctorButton.action = #selector(runDoctor)

        stopButton.bezelStyle = .rounded
        stopButton.target = self
        stopButton.action = #selector(stopSelectedProvider)
        stopButton.isEnabled = false

        statusLabel.textColor = .secondaryLabelColor
        statusLabel.maximumNumberOfLines = 3
        statusLabel.preferredMaxLayoutWidth = 500

        let actionRow = NSStackView(views: [startButton, demoButton, stopButton])
        actionRow.orientation = .horizontal
        actionRow.spacing = 10

        let helpRow = NSStackView(views: [loginButton, doctorButton])
        helpRow.orientation = .horizontal
        helpRow.spacing = 10

        let stack = NSStackView(views: [title, subtitle, providerLabel, providerControl, platformHelp, roomLabel, roomInputField, roomHelpLabel, actionRow, helpRow, statusLabel])
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
            roomInputField.widthAnchor.constraint(equalToConstant: 540),
            roomInputField.heightAnchor.constraint(equalToConstant: 30),
            statusLabel.widthAnchor.constraint(equalToConstant: 540)
        ])
        providerChanged()
    }

    @objc private func providerChanged() {
        let isDouyin = providerControl.selectedSegment == 1
        loginButton.isHidden = !isDouyin
        startButton.title = isDouyin ? "启动抖音看板" : "启动 Bilibili 看板"
        roomInputField.placeholderString = isDouyin
            ? "例如：511304254586 或 https://live.douyin.com/511304254586"
            : "例如：123456 或 https://live.bilibili.com/123456"
        roomHelpLabel.stringValue = isDouyin
            ? "Douyin 真实模式可能需要登录。没有直播间也可以点“先试 Demo”。"
            : "Bilibili 支持房间号或正常直播间链接；需要登录时再按错误提示处理。"
    }

    @objc private func startDemo() {
        launchDemoRequested = true
        providerControl.selectedSegment = 1
        providerChanged()
        startSelectedProvider()
    }

    @objc private func runDoctor() {
        guard loginProcess == nil, !preflightInProgress else { return }
        if projectRoot == nil {
            projectRoot = chooseProjectRoot()
        }
        guard let root = projectRoot else {
            setStatus("请先选择 Bullet-Screen 项目文件夹。", error: true)
            return
        }
        guard let python = findPython(in: root) else {
            showEnvironmentAlert(title: "还不能检查", message: "找不到 Python 3.9+。请先安装 Python，再运行 setup。")
            return
        }
        let doctor = root.appendingPathComponent("scripts/doctor.py")
        guard FileManager.default.fileExists(atPath: doctor.path) else {
            showEnvironmentAlert(title: "项目文件不完整", message: "找不到 scripts/doctor.py。请重新下载完整的 Bullet-Screen 项目。")
            return
        }
        setStatus("正在检查安装环境…")
        let result = LauncherPreflight.runPythonScript(
            at: python.path,
            arguments: ["-u", doctor.path, "--root", root.path],
            currentDirectory: root.path
        )
        if result.ok {
            setStatus("安装环境可以使用。")
            showEnvironmentAlert(title: "安装环境可以使用", message: "Python、项目入口、Playwright、Chromium 和构建工具检查通过。")
        } else {
            setStatus("安装环境还缺东西；请按检查结果处理。", error: true)
            let detail = result.output.isEmpty ? "请先运行 ./scripts/setup.sh。" : String(result.output.suffix(1400))
            showEnvironmentAlert(title: "安装环境需要处理", message: "请先运行 ./scripts/setup.sh。\n\n\(detail)")
        }
    }

    @objc private func loginDouyin() {
        guard loginProcess == nil, !preflightInProgress else { return }
        if projectRoot == nil {
            projectRoot = chooseProjectRoot()
        }
        guard let root = projectRoot else {
            setStatus("请先选择 Bullet-Screen 项目文件夹。", error: true)
            return
        }
        guard let python = findPython(in: root) else {
            setStatus("找不到 Python 3.9+。请先运行 setup。", error: true)
            return
        }
        let loginScript = root.appendingPathComponent("douyin/login.py")
        guard FileManager.default.fileExists(atPath: loginScript.path) else {
            setStatus("找不到 Douyin 登录助手。请确认项目文件完整。", error: true)
            return
        }
        let process = Process()
        process.executableURL = python
        process.arguments = ["-u", loginScript.path]
        process.currentDirectoryURL = root.appendingPathComponent("douyin", isDirectory: true)
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.loginProcess = nil
                self?.loginButton.isEnabled = true
                self?.setStatus("Douyin 登录助手已结束。登录完成后，可以输入直播间再启动。")
            }
        }
        do {
            try process.run()
            loginProcess = process
            loginButton.isEnabled = false
            setStatus("已打开 Douyin 登录窗口。请在窗口中完成登录，再关闭窗口。")
        } catch {
            setStatus("Douyin 登录助手启动失败：\(error.localizedDescription)", error: true)
        }
    }

    private func showEnvironmentAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = title.contains("可以") ? .informational : .warning
        alert.addButton(withTitle: "知道了")
        alert.runModal()
    }

    @objc private func startSelectedProvider() {
        let demoMode = launchDemoRequested
        launchDemoRequested = false
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
            failPreflight("请选择 Bullet-Screen 项目文件夹。")
            return
        }

        let provider = providerControl.selectedSegment == 1 ? "douyin" : "bilibili"
        let roomInput = roomInputField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !demoMode && LauncherOnboarding.normalizeRoomInput(provider: provider, raw: roomInput) == nil {
            failPreflight("请输入直播间链接或房间号。")
            return
        }
        let providerRoot = root.appendingPathComponent(provider, isDirectory: true)
        guard LauncherPreflight.hasServer(root: root.path, provider: provider) else {
            failPreflight("找不到 \(provider)/server.py。请确认项目文件完整。")
            return
        }

        guard let python = findPython(in: root) else {
            failPreflight("找不到 Python 3.9+。请安装支持版本的 Python，再运行 setup。")
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
        if provider == "douyin" && !demoMode,
           let error = douyinRuntimeError(python) {
            failPreflight(error)
            return
        }
        let profileAvailable = provider != "douyin" || demoMode || LauncherPreflight.hasDouyinProfile(root: root.path)

        let identity = lifecycle.beginStart()!
        let process = Process()
        process.executableURL = python
        var arguments = ["-u", "server.py", "--port", String(port)]
        if provider == "douyin" {
            arguments += ["--mode", demoMode ? "demo" : "auto"]
        }
        if !demoMode,
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
        let run = ActiveRun(identity: identity, provider: provider, roomInput: roomInput.isEmpty ? nil : roomInput, demoMode: demoMode, process: process, stdout: stdout, stderr: stderr)
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
            let loginHint = provider == "douyin" && !demoMode && !profileAvailable ? "；如需登录请点“登录 Douyin”" : ""
            setStatus("正在启动 \(provider == "douyin" ? "Douyin" : "Bilibili") 服务\(loginHint)，等待服务…")
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
        let result = runPythonCheck(python, script: "import sys; print(sys.version.split()[0]); raise SystemExit(0 if sys.version_info >= (3, 9) else 1)")
        return result.0 ? nil : "Python 版本不受支持或无法运行：\(result.1)。请安装 Python 3.9+。"
    }

    private func douyinRuntimeError(_ python: URL) -> String? {
        let script = "import os; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); path=p.chromium.executable_path; p.stop(); assert os.path.isfile(path), path; print(path)"
        let result = runPythonCheck(python, script: script)
        return result.0 ? nil : "Douyin 真实模式缺少 Playwright 或 Chromium。请先运行 ./scripts/setup.sh；也可以先试 Demo。\(result.1.isEmpty ? "" : "（\(result.1)）")"
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
            handleLaunchFailure(run, message: "服务没有在规定时间内 ready。请重试，或点击“检查安装环境”。")
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
                    self.setStatus("运行中：\(run.provider == "douyin" ? "Douyin" : "Bilibili") 看板已打开。")
                    if !run.pageOpened {
                        run.pageOpened = true
                        let dashboard = LauncherOnboarding.dashboardURL(
                            port: port,
                            provider: run.provider,
                            roomInput: run.roomInput,
                            demo: run.demoMode
                        ) ?? URL(string: "http://127.0.0.1:\(port)/")!
                        NSWorkspace.shared.open(dashboard)
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
