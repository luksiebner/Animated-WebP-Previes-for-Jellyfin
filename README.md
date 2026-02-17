# Jellyfin Landscape Generator – GUI Edition 🎬

A modern multi-worker tool for generating animated WebP landscape previews for Jellyfin.

## ✨ Features

### Multi-Worker Processing
- Run 2–8 workers in parallel for high CPU utilization
- Automatic load balancing using a job queue
- True multiprocessing architecture

### Moderne GUI
- **Dark Mode Interface** built with CustomTkinter
- Live progress for each worker (progress, ETA, FPS)
- Global progress overview
- Fully configurable settings inside the GUI
- Real-time processing log

### Video-Setup
- Adaptive interval sampling across each video
- Optional bridge sampling for smoother previews
- WebP compression with adjustable quality
- Automatic cleanup of old preview files
- Robust error handling with retry logic

## 📦 Installation

### Requirements
- Python 3.8+
- ffmpeg & ffprobe available in System PATH

### Setup
```bash
pip install customtkinter

python jellyfin_mp4_to_webp_gui.pyw
```

## 🎮 Usage

### GUI-Übersicht

**Configuration**
- **Folder**: Select directory containing MP4 files
- **Workers**: Number of parallel workers (1–8)
- **FPS**: Frames per second for animated WebP
- **Width**: Output width in pixels (height auto-scaled) 
- **Quality**: WebP quality (recommended 40–60)
- **Intervals**: Number of samples per video
- **Clip Length**: Duration of each sample
- **Bridge Settings**: Extra transition samples

**Control**
- **▶ Start Processing**: Start Processing
- **■ Stop**: Stop all workers gracefully

**Workers**
- Pro Worker: Current video, Progress bar, ETA, FPS
- Live updates

**Log**
- Chronological event list
- Errors and success messages
- Per-video progress tracking

### Workflow

1. **Select your media folder** with Browse-Button
2. **Set worker count** (2–4 typical CPUs, 6–8 high-end)
3. **Adjust parameters** experiment with settings
4. **Click Start Processing** 
5. **Monitor live progress**
6. **Finished!** WebP files appear next to the videos

## ⚙️ Parameter-Guide

### Basic Settings
```
FPS: 10              # Higher = more fluid, bigger filesize. More than 15 is overkill
Width: 480           # Standard width for Jellyfin
Quality: 50          # 40-60 is the sweet spot
```

### Sampling
```
Intervals: 10        # 10 Samples taken evenly along video timeline
Clip Length: 0.3     # 0.3s per sample = 3 Frames at 10 FPS
```

### Bridge (Transition-Samples)
```
Bridge %: 5      	 # 5% der Interval-Länge
Bridge Abs: 0        # Alternative: set fixed bridge length 
Bridge Window: 0.03  # 0.03s same as Clip Length but for bridge samples 
```

**Example**: 
  A 100-second video with 10 intervals creates samples at 0s, 10s, 20s… plus additional bridge samples between them.
- Interval = 10s
- Sample at 0s, 10s, 20s, ... 90s (each 0.3s)
- Bridge at 0.5s, 10.5s, 20.5s, ... (each 0.03s)


## 🔧 Technical Details

### Architecture
```
Main Thread (GUI)
├── Config Management
├── UI Updates
└── Monitor Thread
    └── Progress Queue Listener

Worker Processes (1-8x)
├── Job Queue Consumer
├── ffmpeg Subprocess
└── Progress Reporter
```

### Communication
- **Job Queue**: Main → Workers (VideoJob objects)
- **Progress Queue**: Workers → Main (Updates + Completion)
- Thread-safe via `multiprocessing.Queue`

### Error Handling
- ffprobe retry-logic (3x with delay)
- Worker-isolation (a Crash doesn't affect skript as a whole)
- Graceful shutdown at Stop
- Automatic skip of existing files

## 📊 Performance

100 videos (~5 minutes each), 4 workers:
~20–40s per video
~8–10 minutes total processing time
~70–90% CPU utilization

## 🐛 Troubleshooting

### "ffprobe/ffmpeg not found"
```bash
# Windows: ffmpeg not in PATH
# Linux: sudo apt install ffmpeg
# Mac: brew install ffmpeg
```

### "No module named 'customtkinter'"
```bash
pip install customtkinter
```

### GUI not showing
```bash
# Linux: sudo apt install python3-tk
```

### Worker freezing
- Stop processing 
- Reduce worker count
- Lower quality setting
- Identify problematic videos (check Logs)

### Zu hohe CPU-Last
- Reduce worker count
- Lower quality setting

## 💡 Tipps & Tricks
- Tune workers until CPU usage stays around 80-95%
- Start with 2 workers when testing large libraries
- Quality 50 is a good balance for previews

### Quality-Tuning
```
Quality 30: Small but visible artifacts
Quality 50: Balanced (recommended)
Quality 70: very good, but 2x size
Quality 90: Overkill for Preview
```

## 📝 Licence & Credits

CustomTkinter by Tom Schimansky

---

**Have fun creating animated Landscapes! 🚀**
