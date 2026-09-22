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

    public static func hasDatabaseParent(_ path: String) -> Bool {
        let parent = URL(fileURLWithPath: path).standardizedFileURL.deletingLastPathComponent()
        return FileManager.default.fileExists(atPath: parent.path)
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
}
