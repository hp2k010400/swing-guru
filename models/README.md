# Pose model

`pose_landmarker_full.task` is the official MediaPipe float16 pose landmarker
bundle. It is not committed to the repository; download it with:

```powershell
.\.venv\Scripts\python.exe -m swingguru.cli download-model
```

Source:
`https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task`

Referenced from the MediaPipe pose landmarker guide:
`https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker/python`

The engine also accepts any other compatible pose landmarker `.task` file via
`--model`.
