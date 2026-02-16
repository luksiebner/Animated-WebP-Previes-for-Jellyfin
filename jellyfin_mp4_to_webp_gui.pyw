import subprocess
from pathlib import Path
import time
import sys
import multiprocessing as mp
from queue import Empty
from dataclasses import dataclass
from typing import Optional
import customtkinter as ctk
from threading import Thread
import json

# ===============================
# Configuration File
# ===============================

CONFIG_FILE = Path(__file__).parent / "jellyfin_landscape_config.json"

def load_config():
    """Load settings from config file"""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Could not load config: {e}")
    return {}

def save_config(config_dict):
    """Save settings to config file"""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_dict, f, indent=2)
    except Exception as e:
        print(f"Could not save config: {e}")

# ===============================
# Configuration & Data Classes
# ===============================

@dataclass
class VideoJob:
    video_path: Path
    index: int
    total: int
    base_path: Path = None  # For relative path display
    overwrite_existing: bool = False  # Whether to overwrite existing webp files
    video_duration: float = 0  # Duration in seconds for accurate ETA

@dataclass
class ProgressUpdate:
    worker_id: int
    video_name: str
    progress: float
    eta: str
    fps: float
    current_frame: int
    total_frames: int

@dataclass
class JobComplete:
    worker_id: int
    video_name: str
    success: bool
    message: str
    deleted_files: list = None  # List of deleted old landscape files
    video_duration: float = 0  # Duration of processed video for ETA calculation

@dataclass
class WorkerConfig:
    fps: int = 10
    width: int = 480
    quality: int = 50
    interval_count: int = 10
    clip_length: float = 0.3
    bridge_percent: float = 0.05
    bridge_interval_abs: float = 0
    bridge_window: float = 0.03

# ===============================
# Video Processing Functions
# ===============================

def get_video_duration(video_path, retries=3):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]
    
    # Suppress console window on Windows
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    for attempt in range(retries):
        result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
        output = result.stdout.strip()
        if output:
            try:
                return float(output)
            except ValueError:
                pass
        time.sleep(0.5)
    return None

def cleanup_old_landscapes(video_path):
    """Delete old landscape files and return list of deleted files"""
    folder = video_path.parent
    stem = video_path.stem
    deleted = []
    
    for file in folder.glob(f"{stem}-landscape.*"):
        if file.suffix.lower() != ".webp":
            try:
                file.unlink()
                deleted.append(file.name)
            except Exception:
                pass
    
    return deleted

def process_video(job: VideoJob, config: WorkerConfig, worker_id: int, progress_queue: mp.Queue):
    """Process a single video with progress updates"""
    
    video = job.video_path
    output = video.with_name(f"{video.stem}-landscape.webp")
    
    # Calculate relative path for display
    if job.base_path:
        try:
            display_name = str(video.relative_to(job.base_path))
        except ValueError:
            display_name = video.name
    else:
        display_name = video.name
    
    # Check if exists (and skip if not overwriting)
    file_existed = output.exists()
    if file_existed and not job.overwrite_existing:
        progress_queue.put(JobComplete(
            worker_id=worker_id,
            video_name=display_name,
            success=True,
            message="SKIP (exists)",
            video_duration=job.video_duration
        ))
        return
    
    # Get duration
    duration = get_video_duration(video)
    if duration is None:
        progress_queue.put(JobComplete(
            worker_id=worker_id,
            video_name=display_name,
            success=False,
            message="Duration read failed",
            video_duration=job.video_duration
        ))
        return
    
    interval = duration / config.interval_count
    
    # Bridge logic
    if config.bridge_percent > 0:
        bridge_interval = interval * config.bridge_percent
    elif config.bridge_interval_abs > 0:
        bridge_interval = config.bridge_interval_abs
    else:
        bridge_interval = 0
    
    # Estimate frames
    main_frames = config.interval_count * config.clip_length * config.fps
    if bridge_interval > 0:
        bridge_count = duration / bridge_interval
        bridge_frames = bridge_count * config.bridge_window * config.fps
    else:
        bridge_frames = 0
    expected_frames = max(1, int(main_frames + bridge_frames))
    
    # Build filter
    if bridge_interval > 0:
        select_expr = (
            f"lt(mod(t\\,{interval})\\,{config.clip_length})"
            f"+lt(mod(t\\,{bridge_interval})\\,{config.bridge_window})"
        )
    else:
        select_expr = f"lt(mod(t\\,{interval})\\,{config.clip_length})"
    
    vf_filter = (
        f"select='{select_expr}',"
        f"setpts=N/FRAME_RATE/TB,"
        f"fps={config.fps},"
        f"scale={config.width}:-1:flags=lanczos+accurate_rnd"
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-progress", "pipe:1",
        "-nostats",
        "-loglevel", "error",
        "-i", str(video),
        "-an",
        "-vf", vf_filter,
        "-fps_mode", "vfr",
        "-loop", "0",
        "-c:v", "libwebp",
        "-quality", str(config.quality),
        "-compression_level", "6",
        "-preset", "picture",
        str(output)
    ]
    
    try:
        # Suppress console window on Windows
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            startupinfo=startupinfo,
            creationflags=creationflags
        )
        
        start_time = time.time()
        current_frame = 0
        
        for line in process.stdout:
            if "frame=" in line:
                try:
                    parts = line.split("=")
                    if len(parts) >= 2:
                        current_frame = int(parts[1].strip())
                        
                        percent = min(100, (current_frame / expected_frames) * 100)
                        elapsed = time.time() - start_time
                        speed = (current_frame / elapsed) if elapsed > 0 else 0
                        remaining_frames = max(0, expected_frames - current_frame)
                        eta = int(remaining_frames / speed) if speed > 0 else 0
                        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta))
                        
                        progress_queue.put(ProgressUpdate(
                            worker_id=worker_id,
                            video_name=display_name,
                            progress=percent,
                            eta=eta_str,
                            fps=speed,
                            current_frame=current_frame,
                            total_frames=expected_frames
                        ))
                except:
                    pass
        
        process.wait()
        
        if process.returncode == 0:
            deleted_files = cleanup_old_landscapes(video)
            
            # Set message based on whether file was overwritten
            message = "OK (overwritten)" if file_existed else "OK"
            
            progress_queue.put(JobComplete(
                worker_id=worker_id,
                video_name=display_name,
                success=True,
                message=message,
                deleted_files=deleted_files,
                video_duration=job.video_duration
            ))
        else:
            progress_queue.put(JobComplete(
                worker_id=worker_id,
                video_name=display_name,
                success=False,
                message="ffmpeg error",
                video_duration=job.video_duration
            ))
            
    except Exception as e:
        progress_queue.put(JobComplete(
            worker_id=worker_id,
            video_name=display_name,
            success=False,
            message=str(e),
            video_duration=job.video_duration
        ))

# ===============================
# Worker Process
# ===============================

def worker_process(worker_id: int, job_queue: mp.Queue, progress_queue: mp.Queue, config: WorkerConfig):
    """Worker that processes videos from queue"""
    while True:
        try:
            job = job_queue.get(timeout=1)
            if job is None:  # Poison pill
                break
            process_video(job, config, worker_id, progress_queue)
        except Empty:
            continue
        except Exception as e:
            print(f"Worker {worker_id} error: {e}")

# ===============================
# GUI Application
# ===============================

class WorkerPanel(ctk.CTkFrame):
    """Panel showing progress for one worker"""
    
    def __init__(self, master, worker_id):
        super().__init__(master)
        self.worker_id = worker_id
        
        # Title
        self.title = ctk.CTkLabel(self, text=f"Worker {worker_id}", font=("Arial", 14, "bold"))
        self.title.pack(pady=(5,5))
        
        # Video name
        self.video_label = ctk.CTkLabel(self, text="Idle", font=("Arial", 11))
        self.video_label.pack()
        
        # Progress bar
        self.progress = ctk.CTkProgressBar(self, width=300)
        self.progress.pack(pady=5)
        self.progress.set(0)
        
        # Stats
        self.stats_label = ctk.CTkLabel(self, text="0.0% | ETA --:--:-- | 0.0 fps", font=("Arial", 10))
        self.stats_label.pack()
    
    def update_progress(self, update: ProgressUpdate):
        # Shorten very long names
        display_name = update.video_name
        if len(display_name) > 40:
            display_name = "..." + display_name[-37:]
        
        self.video_label.configure(text=display_name)
        self.progress.set(update.progress / 100)
        self.stats_label.configure(
            text=f"{update.progress:.1f}% | ETA {update.eta} | {update.fps:.1f} fps"
        )
    
    def set_idle(self):
        self.video_label.configure(text="Idle")
        self.progress.set(0)
        self.stats_label.configure(text="0.0% | ETA --:--:-- | 0.0 fps")
    
    def set_complete(self, video_name: str):
        # Shorten very long names
        display_name = video_name
        if len(display_name) > 40:
            display_name = "..." + display_name[-37:]
        
        self.video_label.configure(text=f"✓ {display_name}")
        self.progress.set(1.0)

class JellyfinLandscapeGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("Jellyfin Landscape Generator")
        self.geometry("900x800")
        
        # State
        self.workers = []
        self.job_queue = None
        self.progress_queue = None
        self.is_running = False
        self.is_stopping = False  # Track graceful shutdown
        self.active_workers = 0  # Track how many workers are still processing
        self.total_videos = 0
        self.completed_videos = 0
        
        # ETA tracking
        self.batch_start_time = None
        self.video_completion_times = []
        self.total_video_duration = 0  # Total seconds of all videos
        self.processed_video_duration = 0  # Processed seconds so far
        self.video_durations = {}  # Cache: video_path -> duration
        
        # Load saved config
        self.saved_config = load_config()
        
        # UI Setup
        self.setup_ui()
        
        # Apply saved settings
        self.load_settings()
        
    def setup_ui(self):
        # Main container with scrollbar
        main_frame = ctk.CTkScrollableFrame(self, width=850, height=750)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # ========== Configuration Section ==========
        config_frame = ctk.CTkFrame(main_frame)
        config_frame.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(config_frame, text="Configuration", font=("Arial", 16, "bold")).pack(pady=5)
        
        # Folder selection
        folder_frame = ctk.CTkFrame(config_frame)
        folder_frame.pack(fill="x", padx=10, pady=5)
        
        ctk.CTkLabel(folder_frame, text="Folder:").pack(side="left", padx=5)
        self.folder_var = ctk.StringVar(value=str(Path.cwd()))
        self.folder_entry = ctk.CTkEntry(folder_frame, textvariable=self.folder_var, width=400)
        self.folder_entry.pack(side="left", padx=5)
        
        ctk.CTkButton(folder_frame, text="Browse", command=self.browse_folder, width=80).pack(side="left", padx=5)
        
        # Recursive checkbox
        self.recursive_var = ctk.BooleanVar(value=False)
        self.recursive_checkbox = ctk.CTkCheckBox(folder_frame, text="Recursive", variable=self.recursive_var,
                                                    onvalue=True, offvalue=False)
        self.recursive_checkbox.pack(side="left", padx=(10, 5))
        
        # Overwrite checkbox
        self.overwrite_var = ctk.BooleanVar(value=False)
        self.overwrite_checkbox = ctk.CTkCheckBox(folder_frame, text="Overwrite Existing", variable=self.overwrite_var,
                                                    onvalue=True, offvalue=False)
        self.overwrite_checkbox.pack(side="left", padx=5)
        
        # Settings grid
        settings_frame = ctk.CTkFrame(config_frame)
        settings_frame.pack(fill="x", padx=10, pady=10)
        
        # Row 1
        row1 = ctk.CTkFrame(settings_frame)
        row1.pack(fill="x", pady=2)
        
        ctk.CTkLabel(row1, text="Workers:", width=100).pack(side="left", padx=5)
        self.workers_var = ctk.IntVar(value=2)
        ctk.CTkSlider(row1, from_=1, to=8, number_of_steps=7, variable=self.workers_var, width=150).pack(side="left", padx=5)
        self.workers_label = ctk.CTkLabel(row1, text="2", width=30)
        self.workers_label.pack(side="left")
        self.workers_var.trace_add("write", lambda *_: self.workers_label.configure(text=str(self.workers_var.get())))
        
        ctk.CTkLabel(row1, text="FPS:", width=80).pack(side="left", padx=(20,5))
        self.fps_var = ctk.IntVar(value=10)
        ctk.CTkEntry(row1, textvariable=self.fps_var, width=60).pack(side="left", padx=5)
        
        ctk.CTkLabel(row1, text="Width Pxl:", width=80).pack(side="left", padx=(20,5))
        self.width_var = ctk.IntVar(value=480)
        ctk.CTkEntry(row1, textvariable=self.width_var, width=60).pack(side="left", padx=5)
        
        # Row 2
        row2 = ctk.CTkFrame(settings_frame)
        row2.pack(fill="x", pady=2)
        
        ctk.CTkLabel(row2, text="Quality %:", width=100).pack(side="left", padx=5)
        self.quality_var = ctk.IntVar(value=50)
        ctk.CTkEntry(row2, textvariable=self.quality_var, width=60).pack(side="left", padx=5)
        
        ctk.CTkLabel(row2, text="Intervals:", width=80).pack(side="left", padx=(20,5))
        self.intervals_var = ctk.IntVar(value=10)
        ctk.CTkEntry(row2, textvariable=self.intervals_var, width=60).pack(side="left", padx=5)
        
        ctk.CTkLabel(row2, text="Clip Length sec:", width=100).pack(side="left", padx=(20,5))
        self.clip_length_var = ctk.DoubleVar(value=0.3)
        ctk.CTkEntry(row2, textvariable=self.clip_length_var, width=60).pack(side="left", padx=5)
        
        # Row 3 - Bridge settings
        row3 = ctk.CTkFrame(settings_frame)
        row3.pack(fill="x", pady=2)
        
        ctk.CTkLabel(row3, text="Bridge %:", width=100).pack(side="left", padx=5)
        self.bridge_percent_var = ctk.DoubleVar(value=5)
        ctk.CTkEntry(row3, textvariable=self.bridge_percent_var, width=60).pack(side="left", padx=5)
        
        ctk.CTkLabel(row3, text="Bridge Abs sec:", width=100).pack(side="left", padx=(20,5))
        self.bridge_abs_var = ctk.DoubleVar(value=0)
        ctk.CTkEntry(row3, textvariable=self.bridge_abs_var, width=60).pack(side="left", padx=5)
        
        ctk.CTkLabel(row3, text="Bridge Window sec:", width=120).pack(side="left", padx=(20,5))
        self.bridge_window_var = ctk.DoubleVar(value=0.03)
        ctk.CTkEntry(row3, textvariable=self.bridge_window_var, width=60).pack(side="left", padx=5)
        
        # ========== Control Buttons ==========
        control_frame = ctk.CTkFrame(main_frame)
        control_frame.pack(fill="x", padx=10, pady=10)
        
        self.start_button = ctk.CTkButton(control_frame, text="▶ Start Processing", command=self.start_processing, 
                                          fg_color="green", hover_color="darkgreen", font=("Arial", 14, "bold"), height=40)
        self.start_button.pack(side="left", padx=5, expand=True, fill="x")
        
        self.stop_button = ctk.CTkButton(control_frame, text="■ Stop", command=self.stop_processing,
                                         fg_color="red", hover_color="darkred", state="disabled", height=40)
        self.stop_button.pack(side="left", padx=5, expand=True, fill="x")
        
        # Settings info
        settings_info_frame = ctk.CTkFrame(main_frame)
        settings_info_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        ctk.CTkLabel(settings_info_frame, text="💾 Settings are automatically saved", 
                     font=("Arial", 10), text_color="gray").pack(pady=3)
        
        # ========== Overall Progress ==========
        overall_frame = ctk.CTkFrame(main_frame)
        overall_frame.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(overall_frame, text="Overall Progress", font=("Arial", 14, "bold")).pack(pady=5)
        
        # Scan progress (hidden by default)
        self.scan_frame = ctk.CTkFrame(overall_frame)
        self.scan_label = ctk.CTkLabel(self.scan_frame, text="Analyzing video lengths...", font=("Arial", 11))
        self.scan_label.pack()
        
        self.scan_progress = ctk.CTkProgressBar(self.scan_frame, width=400)
        self.scan_progress.pack(pady=5)
        self.scan_progress.set(0)
        
        # Overall progress
        self.overall_label = ctk.CTkLabel(overall_frame, text="0 / 0 videos (0 in queue)", font=("Arial", 12))
        self.overall_label.pack()
        
        self.overall_progress = ctk.CTkProgressBar(overall_frame, width=400)
        self.overall_progress.pack(pady=5)
        self.overall_progress.set(0)
        
        # ========== Worker Panels ==========
        self.workers_frame = ctk.CTkFrame(main_frame)
        self.workers_frame.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(self.workers_frame, text="Workers", font=("Arial", 14, "bold")).pack(pady=5)
        
        self.worker_panels_container = ctk.CTkFrame(self.workers_frame)
        self.worker_panels_container.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.worker_panels = []
        
        # ========== Log ==========
        log_frame = ctk.CTkFrame(main_frame)
        log_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        ctk.CTkLabel(log_frame, text="Log", font=("Arial", 14, "bold")).pack(pady=5)
        
        self.log_text = ctk.CTkTextbox(log_frame, height=150, width=800)
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)
    
    def browse_folder(self):
        folder = ctk.filedialog.askdirectory()
        if folder:
            self.folder_var.set(folder)
            self.save_settings()
    
    def load_settings(self):
        """Load settings from saved config"""
        if not self.saved_config:
            return
        
        try:
            # Set folder
            if 'folder' in self.saved_config:
                folder_path = self.saved_config['folder']
                if Path(folder_path).exists():
                    self.folder_var.set(folder_path)
            
            # Set all parameters
            if 'workers' in self.saved_config:
                self.workers_var.set(self.saved_config['workers'])
            if 'fps' in self.saved_config:
                self.fps_var.set(self.saved_config['fps'])
            if 'width' in self.saved_config:
                self.width_var.set(self.saved_config['width'])
            if 'quality' in self.saved_config:
                self.quality_var.set(self.saved_config['quality'])
            if 'intervals' in self.saved_config:
                self.intervals_var.set(self.saved_config['intervals'])
            if 'clip_length' in self.saved_config:
                self.clip_length_var.set(self.saved_config['clip_length'])
            if 'bridge_percent' in self.saved_config:
                self.bridge_percent_var.set(self.saved_config['bridge_percent'])
            if 'bridge_abs' in self.saved_config:
                self.bridge_abs_var.set(self.saved_config['bridge_abs'])
            if 'bridge_window' in self.saved_config:
                self.bridge_window_var.set(self.saved_config['bridge_window'])
            if 'recursive' in self.saved_config:
                self.recursive_var.set(self.saved_config['recursive'])
            if 'overwrite' in self.saved_config:
                self.overwrite_var.set(self.saved_config['overwrite'])
            
            self.log("✓ Settings loaded from previous session")
        except Exception as e:
            self.log(f"Could not load all settings: {e}")
    
    def save_settings(self):
        """Save current settings to config file"""
        try:
            config = {
                'folder': self.folder_var.get(),
                'workers': self.workers_var.get(),
                'fps': self.fps_var.get(),
                'width': self.width_var.get(),
                'quality': self.quality_var.get(),
                'intervals': self.intervals_var.get(),
                'clip_length': self.clip_length_var.get(),
                'bridge_percent': self.bridge_percent_var.get(),
                'bridge_abs': self.bridge_abs_var.get(),
                'bridge_window': self.bridge_window_var.get(),
                'recursive': self.recursive_var.get(),
                'overwrite': self.overwrite_var.get()
            }
            save_config(config)
        except Exception as e:
            print(f"Could not save settings: {e}")
    
    def log(self, message):
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
    
    def get_config(self) -> WorkerConfig:
        return WorkerConfig(
            fps=self.fps_var.get(),
            width=self.width_var.get(),
            quality=self.quality_var.get(),
            interval_count=self.intervals_var.get(),
            clip_length=self.clip_length_var.get(),
            bridge_percent=self.bridge_percent_var.get() / 100.00,
            bridge_interval_abs=self.bridge_abs_var.get(),
            bridge_window=self.bridge_window_var.get()
        )
    
    def start_processing(self):
        # Save current settings
        self.save_settings()
        
        folder = Path(self.folder_var.get())
        if not folder.exists():
            self.log(f"ERROR: Folder does not exist: {folder}")
            return
        
        # Find videos (recursive or not)
        if self.recursive_var.get():
            mp4_files = list(folder.rglob("*.mp4"))  # Recursive
            self.log(f"Scanning recursively: {folder}")
        else:
            mp4_files = list(folder.glob("*.mp4"))   # Non-recursive
            self.log(f"Scanning folder: {folder}")
        
        if not mp4_files:
            self.log("No MP4 files found")
            return
        
        self.total_videos = len(mp4_files)
        self.completed_videos = 0
        self.is_running = True
        self.is_stopping = False
        
        # Reset ETA tracking
        self.batch_start_time = None
        self.video_completion_times = []
        self.total_video_duration = 0
        self.processed_video_duration = 0
        self.video_durations = {}
        
        self.log(f"Found {self.total_videos} videos")
        if self.recursive_var.get():
            self.log(f"✓ Recursive mode: scanning all subdirectories")
        if self.overwrite_var.get():
            self.log(f"⚠️ Overwrite mode: existing WebP files will be replaced")
        
        # Disable start button during scan
        self.start_button.configure(state="disabled")
        
        # Show scan progress
        self.scan_frame.pack(pady=5)
        self.scan_progress.set(0)
        self.log(f"Analyzing video lengths...")
        
        # Run scan in separate thread to keep GUI responsive
        scan_thread = Thread(target=self._scan_videos_and_start, args=(mp4_files, folder), daemon=True)
        scan_thread.start()
    
    def _scan_videos_and_start(self, mp4_files, folder):
        """Scan all videos for duration, then start processing"""
        total = len(mp4_files)
        
        for idx, video in enumerate(mp4_files, 1):
            duration = get_video_duration(video)
            if duration:
                self.video_durations[str(video)] = duration
                self.total_video_duration += duration
            
            # Update scan progress
            progress = idx / total
            self.after(0, lambda p=progress, i=idx, t=total: self._update_scan_progress(p, i, t))
        
        # Hide scan frame and start processing
        self.after(0, lambda: self._finalize_scan_and_start(mp4_files, folder))
    
    def _update_scan_progress(self, progress, current, total):
        """Update scan progress bar and label"""
        self.scan_progress.set(progress)
        self.scan_label.configure(text=f"Analyzing video lengths... {current}/{total}")
    
    def _finalize_scan_and_start(self, mp4_files, folder):
        """Hide scan UI and start actual processing"""
        self.scan_frame.pack_forget()
        
        # Log scan results
        hours = int(self.total_video_duration // 3600)
        minutes = int((self.total_video_duration % 3600) // 60)
        seconds = int(self.total_video_duration % 60)
        self.log(f"✓ Total video duration: {hours:02d}:{minutes:02d}:{seconds:02d}")
        
        self.log(f"Starting {self.workers_var.get()} workers...")
        
        # Create worker panels
        for widget in self.worker_panels_container.winfo_children():
            widget.destroy()
        self.worker_panels = []
        
        for i in range(self.workers_var.get()):
            panel = WorkerPanel(self.worker_panels_container, i + 1)
            panel.pack(fill="x", pady=5, padx=10)
            self.worker_panels.append(panel)
        
        # Setup queues
        self.job_queue = mp.Queue()
        self.progress_queue = mp.Queue()
        
        # Add jobs with duration info
        for idx, video in enumerate(mp4_files, 1):
            duration = self.video_durations.get(str(video), 0)
            self.job_queue.put(VideoJob(
                video, 
                idx, 
                self.total_videos, 
                base_path=folder,
                overwrite_existing=self.overwrite_var.get(),
                video_duration=duration
            ))
        
        # Add poison pills
        for _ in range(self.workers_var.get()):
            self.job_queue.put(None)
        
        # Start workers
        config = self.get_config()
        self.workers = []
        for i in range(self.workers_var.get()):
            p = mp.Process(target=worker_process, args=(i + 1, self.job_queue, self.progress_queue, config))
            p.start()
            self.workers.append(p)
        
        # Set batch start time when workers actually start
        self.batch_start_time = time.time()
        
        # Update UI
        self.stop_button.configure(state="normal")
        # Keep start button disabled during processing
        
        # Start monitoring thread
        self.monitor_thread = Thread(target=self.monitor_progress, daemon=True)
        self.monitor_thread.start()
    
    def monitor_progress(self):
        """Monitor progress queue and update UI"""
        while self.is_running or self.is_stopping:
            try:
                msg = self.progress_queue.get(timeout=0.1)
                
                if isinstance(msg, ProgressUpdate):
                    # Update worker panel (even during stop)
                    if 0 < msg.worker_id <= len(self.worker_panels):
                        self.after(0, lambda m=msg: self.worker_panels[m.worker_id - 1].update_progress(m))
                
                elif isinstance(msg, JobComplete):
                    # If stopping, just track worker completion
                    if self.is_stopping:
                        # Show as complete if successful, idle if failed
                        if 0 < msg.worker_id <= len(self.worker_panels):
                            if msg.success:
                                self.after(0, lambda m=msg: self.worker_panels[m.worker_id - 1].set_complete(m.video_name))
                            else:
                                self.after(0, lambda m=msg: self.worker_panels[m.worker_id - 1].set_idle())
                        
                        # Still count completed videos during stop
                        if msg.success and msg.message != "SKIP (exists)":
                            self.completed_videos += 1
                        
                        # Check how many workers are still alive
                        alive_workers = sum(1 for w in self.workers if w.is_alive())
                        
                        # Update overall progress with stop status
                        self.after(0, lambda count=alive_workers: self.overall_label.configure(
                            text=f"⏸ Stopping... ({count} worker{'s' if count != 1 else ''} finishing current video{'s' if count != 1 else ''})"
                        ))
                        
                        if alive_workers == 0:
                            # All workers done!
                            self.after(0, self._finish_stop)
                            break
                        
                        continue
                    
                    # Normal processing (not stopping)
                    # Track completion time and video duration
                    # Add processed duration
                    self.processed_video_duration += msg.video_duration
                    
                    elapsed_since_start = time.time() - self.batch_start_time
                    self.video_completion_times.append(elapsed_since_start)
                    
                    self.completed_videos += 1
                    
                    # Update worker panel
                    if 0 < msg.worker_id <= len(self.worker_panels):
                        if msg.success:
                            self.after(0, lambda m=msg: self.worker_panels[m.worker_id - 1].set_complete(m.video_name))
                        else:
                            self.after(0, lambda m=msg: self.worker_panels[m.worker_id - 1].set_idle())
                    
                    # Log
                    status = "✓" if msg.success else "✗"
                    log_msg = f"{status} [{self.completed_videos}/{self.total_videos}] {msg.video_name} - {msg.message}"
                    
                    # Add info about deleted files
                    if msg.success and msg.deleted_files:
                        deleted_list = ", ".join(msg.deleted_files)
                        log_msg += f" (deleted: {deleted_list})"
                    
                    self.after(0, lambda m=log_msg: self.log(m))
                    
                    # Calculate duration-based ETA
                    remaining = self.total_videos - self.completed_videos
                    remaining_duration = self.total_video_duration - self.processed_video_duration
                    
                    # Need at least 10 seconds elapsed OR 2 videos to calculate accurate ETA
                    if self.completed_videos >= 2 or elapsed_since_start >= 10.0:
                        # Calculate processing rate: seconds of video per second of real time
                        processing_rate = self.processed_video_duration / elapsed_since_start
                        
                        if processing_rate > 0:
                            eta_seconds = remaining_duration / processing_rate
                            eta_str = time.strftime("%H:%M:%S", time.gmtime(int(eta_seconds)))
                            
                            # Calculate speed multiplier (how many times faster than playback)
                            speed_multiplier = processing_rate  # Already in seconds/second
                            
                            # Update overall progress with accurate ETA
                            progress = self.completed_videos / self.total_videos
                            self.after(0, lambda: self.overall_progress.set(progress))
                            self.after(0, lambda: self.overall_label.configure(
                                text=f"{self.completed_videos} / {self.total_videos} videos ({remaining} remaining) | ETA: {eta_str} ({speed_multiplier:.0f}x speed)"
                            ))
                        else:
                            # Fallback if rate is 0
                            progress = self.completed_videos / self.total_videos
                            self.after(0, lambda: self.overall_progress.set(progress))
                            self.after(0, lambda: self.overall_label.configure(
                                text=f"{self.completed_videos} / {self.total_videos} videos ({remaining} remaining) | ETA: calculating..."
                            ))
                    else:
                        # First video or not enough time - no ETA yet
                        progress = self.completed_videos / self.total_videos
                        self.after(0, lambda: self.overall_progress.set(progress))
                        self.after(0, lambda: self.overall_label.configure(
                            text=f"{self.completed_videos} / {self.total_videos} videos ({remaining} remaining) | ETA: calculating..."
                        ))
                    
                    # Check if done
                    if self.completed_videos >= self.total_videos:
                        self.after(0, self.processing_complete)
                
            except Empty:
                # During graceful stop, check if all workers are done
                if self.is_stopping:
                    alive_workers = sum(1 for w in self.workers if w.is_alive())
                    if alive_workers == 0:
                        self.after(0, self._finish_stop)
                        break
                continue
            except Exception as e:
                self.after(0, lambda: self.log(f"Monitor error: {e}"))
    
    def processing_complete(self):
        elapsed = time.time() - self.batch_start_time
        elapsed_str = time.strftime("%H:%M:%S", time.gmtime(int(elapsed)))
        
        # Calculate final processing rate
        if elapsed > 0:
            processing_rate = self.processed_video_duration / elapsed
            speed_multiplier = processing_rate  # seconds of video per second of real time
            
            self.log(f"\n✓ All done! Processed {self.completed_videos}/{self.total_videos} videos")
            self.log(f"Total time: {elapsed_str}")
            self.log(f"Processing speed: {speed_multiplier:.0f}x faster than playback")
        else:
            self.log(f"\n✓ All done! Processed {self.completed_videos}/{self.total_videos} videos")
        
        self.is_running = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        
        # Set all workers idle
        for panel in self.worker_panels:
            panel.set_idle()
    
    def stop_processing(self):
        if not self.is_running:
            return
        
        self.is_stopping = True
        self.is_running = False
        
        # Update button to orange "Stopping..." state
        self.stop_button.configure(
            text="⏸ Stopping...", 
            state="disabled",
            fg_color="orange",
            hover_color="darkorange"
        )
        
        # Count active workers (those that are currently processing)
        self.active_workers = len(self.workers)
        
        # Immediately show stopping status in UI
        self.overall_label.configure(
            text=f"⏸ Stopping... ({self.active_workers} worker{'s' if self.active_workers != 1 else ''} finishing current video{'s' if self.active_workers != 1 else ''})"
        )
        
        self.log(f"⏸ Stopping gracefully... waiting for {self.active_workers} worker{'s' if self.active_workers != 1 else ''} to finish")
        
        # Clear the job queue (remove all pending jobs)
        while not self.job_queue.empty():
            try:
                self.job_queue.get_nowait()
            except:
                break
        
        # Send poison pills to all workers (so they stop after current job)
        for _ in range(len(self.workers)):
            self.job_queue.put(None)
        
        # Monitor thread will handle the rest and call _finish_stop when all workers are done
    
    def _finish_stop(self):
        """Called when all workers have finished during graceful shutdown"""
        # Calculate how many videos were completed vs cancelled
        completed = self.completed_videos
        total = self.total_videos
        cancelled = total - completed
        
        if cancelled > 0:
            self.log(f"✓ Stopped gracefully - {completed} videos completed, {cancelled} cancelled")
        else:
            self.log(f"✓ Stopped gracefully - {completed} videos completed")
        
        # Wait for all worker processes to fully terminate
        for worker in self.workers:
            worker.join(timeout=1)
        
        self.workers = []
        self.is_stopping = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(
            text="■ Stop", 
            state="disabled",
            fg_color="red",
            hover_color="darkred"
        )
        
        # Update overall progress to show stopped state
        self.overall_label.configure(
            text=f"⏸ Stopped - {completed} / {total} videos completed ({cancelled} cancelled)"
        )
        
        # Set all workers idle
        for panel in self.worker_panels:
            panel.set_idle()

# ===============================
# Main
# ===============================

if __name__ == "__main__":
    # Set appearance
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    
    # Required for multiprocessing on Windows
    mp.freeze_support()
    
    app = JellyfinLandscapeGUI()
    app.mainloop()
