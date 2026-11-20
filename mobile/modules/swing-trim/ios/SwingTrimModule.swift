// Trims the chosen part of a swing video WITHOUT re-encoding (AVAssetExportPresetPassthrough):
// same codec, resolution, bit rate and frame rate (60 fps stays 60 fps), so only the selected
// seconds are uploaded. Passthrough starts at the keyframe at or before `startMs`, so the file can
// hold a few extra frames of set-up, which the analysis welcomes.
//
// motionProfile samples the clip at a few frames per second, shrinks each frame to 32x32 grey and
// returns the mean absolute difference between neighbours: the swing is the clip's biggest burst
// of motion, which is where the trim screen places its default selection.
import AVFoundation
import CoreGraphics
import ExpoModulesCore

public class SwingTrimModule: Module {
  public func definition() -> ModuleDefinition {
    Name("SwingTrim")

    AsyncFunction("trim") { (uri: String, startMs: Double, endMs: Double, promise: Promise) in
      guard let source = URL(string: uri), endMs > startMs else {
        promise.reject("ERR_TRIM_INPUT", "Invalid video or range")
        return
      }
      let asset = AVURLAsset(url: source)
      guard let session = AVAssetExportSession(asset: asset, presetName: AVAssetExportPresetPassthrough) else {
        promise.reject("ERR_TRIM_UNSUPPORTED", "This video can't be trimmed")
        return
      }
      let type: AVFileType = session.supportedFileTypes.contains(.mov) ? .mov : .mp4
      let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("swing-trims", isDirectory: true)
      try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
      let output = folder.appendingPathComponent("\(UUID().uuidString).\(type == .mov ? "mov" : "mp4")")
      session.outputURL = output
      session.outputFileType = type
      session.shouldOptimizeForNetworkUse = true
      session.timeRange = CMTimeRange(
        start: CMTime(seconds: startMs / 1000, preferredTimescale: 600),
        end: CMTime(seconds: endMs / 1000, preferredTimescale: 600))
      session.exportAsynchronously {
        switch session.status {
        case .completed:
          let size = (try? FileManager.default.attributesOfItem(atPath: output.path)[.size] as? NSNumber)?.intValue ?? 0
          let duration = AVURLAsset(url: output).duration.seconds
          promise.resolve([
            "uri": output.absoluteString,
            "size": size,
            "durationMs": duration.isFinite ? duration * 1000 : endMs - startMs,
            "mimeType": type == .mov ? "video/quicktime" : "video/mp4",
          ])
        default:
          promise.reject("ERR_TRIM_FAILED", session.error?.localizedDescription ?? "Trimming failed")
        }
      }
    }

    // Only for a trimmed clip that is still too big to upload (storage refuses large single uploads):
    // re-encodes it once to 1080p H.264 (the analysis needs a long side of at least 960 px). Clips
    // that already fit never come here, so their passthrough quality is untouched.
    AsyncFunction("fitToSize") { (uri: String, promise: Promise) in
      guard let source = URL(string: uri) else {
        promise.reject("ERR_FIT_INPUT", "Invalid video")
        return
      }
      let asset = AVURLAsset(url: source)
      guard let session = AVAssetExportSession(asset: asset, presetName: AVAssetExportPreset1920x1080) else {
        promise.reject("ERR_FIT_UNSUPPORTED", "This video can't be resized")
        return
      }
      let type: AVFileType = session.supportedFileTypes.contains(.mp4) ? .mp4 : .mov
      let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("swing-trims", isDirectory: true)
      try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
      let output = folder.appendingPathComponent("\(UUID().uuidString).\(type == .mov ? "mov" : "mp4")")
      session.outputURL = output
      session.outputFileType = type
      session.shouldOptimizeForNetworkUse = true
      session.exportAsynchronously {
        switch session.status {
        case .completed:
          let size = (try? FileManager.default.attributesOfItem(atPath: output.path)[.size] as? NSNumber)?.intValue ?? 0
          let duration = AVURLAsset(url: output).duration.seconds
          promise.resolve([
            "uri": output.absoluteString,
            "size": size,
            "durationMs": duration.isFinite ? duration * 1000 : asset.duration.seconds * 1000,
            "mimeType": type == .mov ? "video/quicktime" : "video/mp4",
          ])
        default:
          promise.reject("ERR_FIT_FAILED", session.error?.localizedDescription ?? "Resizing failed")
        }
      }
    }

    AsyncFunction("motionProfile") { (uri: String, samplesPerSecond: Double, promise: Promise) in
      guard let source = URL(string: uri) else {
        promise.reject("ERR_MOTION_INPUT", "Invalid video")
        return
      }
      let asset = AVURLAsset(url: source)
      let duration = asset.duration.seconds
      guard duration.isFinite, duration > 0 else {
        promise.reject("ERR_MOTION_INPUT", "Unreadable video")
        return
      }
      let rate = max(1, min(samplesPerSecond, 15))
      let count = max(2, Int(duration * rate))
      let times = (0..<count).map { NSValue(time: CMTime(seconds: Double($0) / rate, preferredTimescale: 600)) }
      let generator = AVAssetImageGenerator(asset: asset)
      generator.appliesPreferredTrackTransform = true
      generator.maximumSize = CGSize(width: 96, height: 96)
      let tolerance = CMTime(seconds: 0.5 / rate, preferredTimescale: 600)
      generator.requestedTimeToleranceBefore = tolerance
      generator.requestedTimeToleranceAfter = tolerance

      var frames = [[UInt8]?](repeating: nil, count: count)
      var finished = 0
      let lock = NSLock()
      generator.generateCGImagesAsynchronously(forTimes: times) { requested, image, _, _, _ in
        let index = Int((requested.seconds * rate).rounded())
        let grey = image.flatMap(SwingTrimModule.grey32)
        lock.lock()
        if index >= 0 && index < count { frames[index] = grey }
        finished += 1
        let done = finished == count
        lock.unlock()
        guard done else { return }
        var values = [Double](repeating: 0, count: count)
        for i in 1..<count {
          guard let a = frames[i - 1], let b = frames[i] else { continue }
          var total = 0
          for p in 0..<a.count { total += abs(Int(a[p]) - Int(b[p])) }
          values[i] = Double(total) / Double(a.count)
        }
        promise.resolve(["samplesPerSecond": rate, "durationMs": duration * 1000, "values": values])
      }
    }
  }

  private static func grey32(_ image: CGImage) -> [UInt8]? {
    let side = 32
    var pixels = [UInt8](repeating: 0, count: side * side)
    let drawn: Bool = pixels.withUnsafeMutableBytes { buffer in
      guard let context = CGContext(
        data: buffer.baseAddress, width: side, height: side, bitsPerComponent: 8, bytesPerRow: side,
        space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)
      else { return false }
      context.interpolationQuality = .low
      context.draw(image, in: CGRect(x: 0, y: 0, width: side, height: side))
      return true
    }
    return drawn ? pixels : nil
  }
}
