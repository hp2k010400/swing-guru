// Android twin of ios/SwingTrimModule.swift: trim = remux the selected range of the video track
// (MediaExtractor -> MediaMuxer, no re-encode, original frame rate), starting at the keyframe at or
// before the start. motionProfile = mean absolute difference of 32x32 grey frames.
package expo.modules.swingtrim

import android.graphics.Bitmap
import android.graphics.Color
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import android.media.MediaMuxer
import android.net.Uri
import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.io.File
import java.nio.ByteBuffer
import java.util.UUID
import kotlin.math.abs

class SwingTrimModule : Module() {
  private val context get() = requireNotNull(appContext.reactContext)

  override fun definition() = ModuleDefinition {
    Name("SwingTrim")

    AsyncFunction("trim") { uri: String, startMs: Double, endMs: Double ->
      if (endMs <= startMs) throw CodedException("ERR_TRIM_INPUT", "Invalid range", null)
      val extractor = MediaExtractor()
      extractor.setDataSource(context, Uri.parse(uri), null)
      var track = -1
      var format: MediaFormat? = null
      for (i in 0 until extractor.trackCount) {
        val f = extractor.getTrackFormat(i)
        if (f.getString(MediaFormat.KEY_MIME)?.startsWith("video/") == true) { track = i; format = f; break }
      }
      if (track < 0 || format == null) throw CodedException("ERR_TRIM_UNSUPPORTED", "No video track", null)
      extractor.selectTrack(track)
      val folder = File(context.cacheDir, "swing-trims").apply { mkdirs() }
      val output = File(folder, "${UUID.randomUUID()}.mp4")
      val muxer = MediaMuxer(output.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
      if (format.containsKey(MediaFormat.KEY_ROTATION)) muxer.setOrientationHint(format.getInteger(MediaFormat.KEY_ROTATION))
      val out = muxer.addTrack(format)
      muxer.start()
      val capacity = if (format.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE)) format.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE) else 8 * 1024 * 1024
      val buffer = ByteBuffer.allocate(capacity)
      val info = MediaCodec.BufferInfo()
      val startUs = (startMs * 1000).toLong()
      val endUs = (endMs * 1000).toLong()
      extractor.seekTo(startUs, MediaExtractor.SEEK_TO_PREVIOUS_SYNC)
      var firstUs = -1L
      var lastUs = 0L
      while (true) {
        val size = extractor.readSampleData(buffer, 0)
        if (size < 0) break
        val time = extractor.sampleTime
        if (time > endUs) break
        if (firstUs < 0) firstUs = time
        info.set(0, size, time - firstUs,
          if (extractor.sampleFlags and MediaExtractor.SAMPLE_FLAG_SYNC != 0) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0)
        muxer.writeSampleData(out, buffer, info)
        lastUs = time
        extractor.advance()
      }
      muxer.stop()
      muxer.release()
      extractor.release()
      mapOf(
        "uri" to Uri.fromFile(output).toString(),
        "size" to output.length().toDouble(),
        "durationMs" to (lastUs - firstUs).coerceAtLeast(0) / 1000.0,
        "mimeType" to "video/mp4",
      )
    }

    AsyncFunction("motionProfile") { uri: String, samplesPerSecond: Double ->
      val retriever = MediaMetadataRetriever()
      try {
        retriever.setDataSource(context, Uri.parse(uri))
        val durationMs = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toDoubleOrNull() ?: 0.0
        if (durationMs <= 0) throw CodedException("ERR_MOTION_INPUT", "Unreadable video", null)
        val rate = samplesPerSecond.coerceIn(1.0, 15.0)
        val count = maxOf(2, (durationMs / 1000 * rate).toInt())
        var previous: IntArray? = null
        val values = DoubleArray(count)
        for (i in 0 until count) {
          val frame = retriever.getScaledFrameAtTime((i / rate * 1_000_000).toLong(), MediaMetadataRetriever.OPTION_CLOSEST, 96, 96)
          val grey = frame?.let { grey32(it) }
          if (grey != null && previous != null) {
            var total = 0L
            for (p in grey.indices) total += abs(grey[p] - previous[p])
            values[i] = total.toDouble() / grey.size
          }
          previous = grey
        }
        mapOf("samplesPerSecond" to rate, "durationMs" to durationMs, "values" to values.toList())
      } finally {
        retriever.release()
      }
    }
  }

  private fun grey32(bitmap: Bitmap): IntArray {
    val small = Bitmap.createScaledBitmap(bitmap, 32, 32, false)
    val pixels = IntArray(32 * 32)
    small.getPixels(pixels, 0, 32, 0, 0, 32, 32)
    return IntArray(pixels.size) { (Color.red(pixels[it]) * 299 + Color.green(pixels[it]) * 587 + Color.blue(pixels[it]) * 114) / 1000 }
  }
}
