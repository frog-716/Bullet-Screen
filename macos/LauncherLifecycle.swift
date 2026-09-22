import Foundation
import Darwin

public enum LauncherState: String, Equatable {
    case stopped
    case starting
    case waitingForReady
    case running
    case stopping
    case failed
}

public struct LaunchRunIdentity: Equatable {
    public let generation: UInt64
    public let token: UUID

    public init(generation: UInt64, token: UUID = UUID()) {
        self.generation = generation
        self.token = token
    }
}

public final class LauncherLifecycleModel {
    public private(set) var state: LauncherState = .stopped
    public private(set) var currentRun: LaunchRunIdentity?

    private var nextGeneration: UInt64 = 0

    @discardableResult
    public func beginStart() -> LaunchRunIdentity? {
        guard state == .stopped || state == .failed else { return nil }
        nextGeneration += 1
        let run = LaunchRunIdentity(generation: nextGeneration)
        currentRun = run
        state = .starting
        return run
    }

    @discardableResult
    public func markWaitingForReady(_ run: LaunchRunIdentity) -> Bool {
        guard currentRun == run, state == .starting else { return false }
        state = .waitingForReady
        return true
    }

    @discardableResult
    public func markReady(_ run: LaunchRunIdentity) -> Bool {
        guard currentRun == run, state == .waitingForReady else { return false }
        state = .running
        return true
    }

    @discardableResult
    public func beginStop(for run: LaunchRunIdentity) -> Bool {
        guard currentRun == run,
              state == .starting || state == .waitingForReady || state == .running
        else { return false }
        state = .stopping
        return true
    }

    @discardableResult
    public func markStopTimedOut(for run: LaunchRunIdentity) -> Bool {
        guard currentRun == run, state == .stopping else { return false }
        return true
    }

    @discardableResult
    public func finishStop(for run: LaunchRunIdentity) -> Bool {
        guard currentRun == run, state == .stopping else { return false }
        currentRun = nil
        state = .stopped
        return true
    }

    @discardableResult
    public func fail(_ run: LaunchRunIdentity) -> Bool {
        guard currentRun == run else { return false }
        currentRun = nil
        state = .failed
        return true
    }

    public func accepts(_ run: LaunchRunIdentity) -> Bool {
        guard currentRun == run else { return false }
        return state != .stopping && state != .stopped && state != .failed
    }
}

public enum LauncherPreflight {
    public static func parsePort(_ raw: String?) -> Int? {
        let value = raw ?? "0"
        guard let port = Int(value), port == 0 || (1...65535).contains(port) else { return nil }
        return port
    }

    public static func isExecutable(_ path: String) -> Bool {
        FileManager.default.isExecutableFile(atPath: path)
    }

    public static func hasServer(root: String, provider: String) -> Bool {
        let path = URL(fileURLWithPath: root).appendingPathComponent(provider, isDirectory: true).appendingPathComponent("server.py")
        return FileManager.default.fileExists(atPath: path.path)
    }

    public static func isProjectRoot(root: String) -> Bool {
        hasServer(root: root, provider: "bilibili") && hasServer(root: root, provider: "douyin")
    }

    public static func hasDatabaseParent(_ path: String) -> Bool {
        let parent = URL(fileURLWithPath: path).standardizedFileURL.deletingLastPathComponent()
        return FileManager.default.fileExists(atPath: parent.path)
    }

    public static func hasDouyinProfile(root: String) -> Bool {
        var isDirectory = ObjCBool(false)
        let path = URL(fileURLWithPath: root)
            .appendingPathComponent("douyin/data/browser-profile", isDirectory: true).path
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory) && isDirectory.boolValue
    }

    public static func isPortAvailable(_ port: Int) -> Bool {
        guard port != 0 else { return true }
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return false }
        defer { close(descriptor) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(UInt16(port).bigEndian)
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        return withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
    }

    public static func runPythonCheck(at path: String, script: String) -> (ok: Bool, output: String) {
        let check = Process()
        let output = Pipe()
        check.executableURL = URL(fileURLWithPath: path)
        check.arguments = ["-c", script]
        check.standardOutput = output
        check.standardError = output
        do {
            try check.run()
            check.waitUntilExit()
        } catch {
            return (false, error.localizedDescription)
        }
        let text = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return (check.terminationStatus == 0, text.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    public static func runPythonScript(at path: String, arguments: [String], currentDirectory: String) -> (ok: Bool, output: String) {
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        process.currentDirectoryURL = URL(fileURLWithPath: currentDirectory, isDirectory: true)
        process.standardOutput = output
        process.standardError = output
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return (false, error.localizedDescription)
        }
        let text = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return (process.terminationStatus == 0, text.trimmingCharacters(in: .whitespacesAndNewlines))
    }
}

public enum LauncherOnboarding {
    public static func normalizeRoomInput(provider: String, raw: String) -> String? {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return nil }
        if value.range(of: "^[0-9]+$", options: .regularExpression) != nil {
            return value
        }
        guard let components = URLComponents(string: value),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = components.host?.lowercased()
        else { return nil }
        let acceptedHosts: Set<String> = provider == "bilibili"
            ? ["live.bilibili.com", "www.live.bilibili.com"]
            : ["live.douyin.com"]
        guard acceptedHosts.contains(host) else { return nil }
        let roomID = components.path.split(separator: "/").first.map(String.init) ?? ""
        guard roomID.range(of: "^[0-9]+$", options: .regularExpression) != nil else { return nil }
        return provider == "bilibili" ? roomID : value
    }

    public static func dashboardURL(port: Int, provider: String, roomInput: String?, demo: Bool) -> URL? {
        guard let normalized = URLComponents(string: "http://127.0.0.1:\(port)/") else { return nil }
        var components = normalized
        var queryItems: [URLQueryItem] = []
        if let roomInput, let room = normalizeRoomInput(provider: provider, raw: roomInput) {
            if room.range(of: "^[0-9]+$", options: .regularExpression) != nil {
                queryItems.append(URLQueryItem(name: "room_id", value: room))
            } else {
                queryItems.append(URLQueryItem(name: "room_url", value: room))
            }
        }
        if demo {
            queryItems.append(URLQueryItem(name: "demo", value: "1"))
            queryItems.append(URLQueryItem(name: "autostart", value: "1"))
        }
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        return components.url
    }
}
