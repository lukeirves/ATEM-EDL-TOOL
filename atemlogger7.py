import time
import logging
import csv
import json
import os
import re
import tempfile
from pathlib import Path
from PyATEMMax import ATEMMax
import socket
import threading
from PyQt6.QtCore import QThread, QTimer, QSettings, pyqtSignal, Qt, QEvent, QPropertyAnimation, QEasingCurve
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QFileDialog, QMessageBox, QGroupBox, QTableWidget, QTableWidgetItem, QFrame, QCheckBox, QSpinBox, QComboBox, QScrollArea, QLCDNumber
from PyQt6.QtGui import QColor, QBrush, QFontDatabase, QPalette, QPainter, QPen

# Basic logging configuration
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("ATEMLogger")
APP_VERSION = "0.5.4"


def source_id(source):
    """Use the protocol ID for comparisons across ATEMConstant and integer values."""
    return source.value if hasattr(source, 'value') else source


def source_name(atem, source):
    """Return the label configured on the switcher, with a readable fallback."""
    try:
        properties = atem.inputProperties[source]
        return properties.longName or properties.shortName or str(source)
    except (KeyError, AttributeError, TypeError):
        return str(source)


def switcher_has_input_names(atem):
    for number in range(1, 41):
        source = getattr(atem.atem.videoSources, f"input{number}")
        if source_name(atem, source) != str(source):
            return True
    return False


def connect_switcher(atem, ip):
    """Wait for the source data needed by this app.

    Large Constellation models can provide usable state without PyATEMMax ever
    marking its full initialization handshake complete.
    """
    atem.connect(ip)
    if not atem.waitForConnection(timeout=5, waitForFullHandshake=False):
        raise ConnectionError("ATEM did not respond at this IP address")

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for number in range(1, 41):
            source = getattr(atem.atem.videoSources, f"input{number}")
            if source_name(atem, source) != str(source):
                # Give the remaining input labels a moment to arrive.
                time.sleep(0.5)
                return
        time.sleep(0.05)
    raise ConnectionError("ATEM responded, but did not send its input names")

# Function to generate an EDL file with unique identifiers for each clip
TIMECODE_RATES = {
    "23.976": (24, 0), "24": (24, 0), "25": (25, 0),
    "29.97 NDF": (30, 0), "29.97 DF": (30, 2), "30": (30, 0),
    "47.952": (48, 0), "48": (48, 0), "50": (50, 0),
    "59.94 NDF": (60, 0), "59.94 DF": (60, 4), "60": (60, 0),
}


def suggested_camera_number(source_name):
    """Suggest a CuePilot camera number from common ATEM input labels."""
    source_name = source_name.strip()
    match = re.search(r"(?:^|\b)(?:CAM(?:ERA)?\s*|INPUT\s*)?(\d{1,2})$", source_name, re.IGNORECASE)
    if match:
        number = int(match.group(1))
        if 1 <= number <= 48:
            return number
    return None


def valid_camera_number(value):
    return type(value) is int and 1 <= value <= 48


EDL_NAME_LIMIT = 8


def edl_clip_name(source, camera):
    """Keep the CuePilot camera suffix visible within a classic EDL name."""
    suffix = f"_{camera}"
    name = " ".join(source.split())
    if name.endswith(suffix):
        name = name[:-len(suffix)]
    return f"{name[:EDL_NAME_LIMIT - len(suffix)]}{suffix}"


def frames_per_day(rate):
    nominal, dropped = TIMECODE_RATES[rate]
    return nominal * 86400 - dropped * (1440 - 144)


def timecode_to_frames(timecode, rate="24"):
    nominal, dropped = TIMECODE_RATES[rate]
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}[:;]\d{2}", timecode.strip()):
        raise ValueError(f"Invalid timecode format: {timecode!r}. Use HH:MM:SS:FF.")
    hours, minutes, seconds, frames = map(int, timecode.replace(';', ':').split(':'))
    if not (0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 60 and 0 <= frames < nominal):
        raise ValueError(f"{timecode} is outside {rate} fps (frames must be 00–{nominal - 1:02}).")
    if dropped and minutes % 10 and seconds == 0 and frames < dropped:
        raise ValueError(f"Invalid drop-frame timecode: {timecode}")
    total_minutes = hours * 60 + minutes
    return ((total_minutes * 60 + seconds) * nominal + frames
            - dropped * (total_minutes - total_minutes // 10))


class TimecodeRunDetector:
    """Require two timely forward advances before an armed logger starts."""
    def __init__(self, rate):
        self.rate = rate
        self.previous_frames = None
        self.last_advance_at = None
        self.advance_count = 0

    def reset(self):
        self.previous_frames = None
        self.last_advance_at = None
        self.advance_count = 0

    def observe(self, frames, now):
        if self.previous_frames is None:
            self.previous_frames = frames
            return False
        advance = (frames - self.previous_frames) % frames_per_day(self.rate)
        if advance == 0:
            if self.last_advance_at is not None and now - self.last_advance_at > 2:
                self.advance_count = 0
            return False
        self.previous_frames = frames
        if advance > TIMECODE_RATES[self.rate][0] * 2:
            self.advance_count = 0
            self.last_advance_at = None
            return False
        if self.last_advance_at is None or now - self.last_advance_at > 2:
            self.advance_count = 1
        else:
            self.advance_count += 1
        self.last_advance_at = now
        return self.advance_count >= 2


def frames_to_timecode(total_frames, rate="24"):
    nominal, dropped = TIMECODE_RATES[rate]
    total_frames %= frames_per_day(rate)
    if dropped:
        frames_per_ten_minutes = nominal * 600 - dropped * 9
        ten_minute_blocks, remainder = divmod(total_frames, frames_per_ten_minutes)
        skipped = dropped * 9 * ten_minute_blocks
        if remainder >= dropped:
            skipped += dropped * ((remainder - dropped) // (nominal * 60 - dropped))
        total_frames += skipped
    hours, remainder = divmod(total_frames, 3600 * nominal)
    minutes, remainder = divmod(remainder, 60 * nominal)
    seconds, frames = divmod(remainder, nominal)
    separator = ';' if dropped else ':'
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{frames:02}"


def duration_timecode(start, end, rate="24"):
    return frames_to_timecode((timecode_to_frames(end, rate) - timecode_to_frames(start, rate)) % frames_per_day(rate), rate)


def ripple_clips(clips, row, field, new_timecode, rate):
    """Ripple one adjacent edit point and report clips invalidated by the move."""
    if field not in ('start', 'end'):
        raise ValueError('Only Start or End timecodes can be rippled.')
    if not 0 <= row < len(clips):
        raise IndexError('The edited clip row is outside the sequence.')
    day = frames_per_day(rate)
    origin = timecode_to_frames(clips[0]['start'], rate)
    starts = []
    ends = []
    for index, clip in enumerate(clips):
        try:
            start_frame = timecode_to_frames(clip['start'], rate)
            end_frame = timecode_to_frames(clip['end'], rate)
        except ValueError as e:
            raise ValueError(f"Row {index + 1} cannot be rippled: {e}") from e
        start_offset = (start_frame - origin) % day
        duration = (end_frame - start_frame) % day
        starts.append(start_offset)
        ends.append(start_offset + duration)

    def nearest_delta(new_frame, old_frame):
        delta = (new_frame - old_frame) % day
        return delta - day if delta > day // 2 else delta

    new_frame = timecode_to_frames(new_timecode, rate)
    updated = [clip.copy() for clip in clips]
    updated[row][field] = new_timecode
    removed = []
    if field == 'start':
        old_frame = timecode_to_frames(clips[row]['start'], rate)
        boundary = starts[row] + nearest_delta(new_frame, old_frame)
        previous = row - 1
        while previous >= 0 and starts[previous] >= boundary:
            removed.append(previous)
            previous -= 1
        if previous >= 0:
            updated[previous]['end'] = frames_to_timecode(boundary, rate)
    else:
        old_frame = timecode_to_frames(clips[row]['end'], rate)
        boundary = ends[row] + nearest_delta(new_frame, old_frame)
        following = row + 1
        while following < len(clips) and ends[following] <= boundary:
            removed.append(following)
            following += 1
        if following < len(clips):
            updated[following]['start'] = frames_to_timecode(boundary, rate)
    removed_set = set(removed)
    return [clip for index, clip in enumerate(updated) if index not in removed_set], removed


def display_duration(start, end, rate):
    try:
        return duration_timecode(start, end, rate)
    except ValueError:
        return "Invalid for selected FPS"


def export_timecode(timecode, compensation_frames=0, offset_hours=0, rate="24"):
    nominal, dropped = TIMECODE_RATES[rate]
    frames_per_hour = nominal * 3600 - dropped * 54
    return frames_to_timecode(timecode_to_frames(timecode, rate) + compensation_frames + offset_hours * frames_per_hour, rate)


def generate_edl(clips, file_path, compensation_frames=0, offset_hours=0, rate="24"):
    for row, clip in enumerate(clips, 1):
        if not valid_camera_number(clip.get('camera')):
            raise ValueError(f"Row {row} needs a CuePilot camera number from 1 to 48.")
    with open(file_path, 'w') as edl_file:
        # EDL header
        edl_file.write("TITLE: ATEM Program Output\n")
        edl_file.write("FCM: DROP FRAME\n" if TIMECODE_RATES[rate][1] else "FCM: NON-DROP FRAME\n")
        edl_file.write(f"* ATEM LOGGER TIMECODE RATE: {rate}\n")
        
        for i, clip in enumerate(clips):
            start_timecode = clip['start']
            end_timecode = clip['end']

            # If compensation is enabled, adjust the timecode
            start_timecode = export_timecode(start_timecode, compensation_frames, offset_hours, rate)
            end_timecode = export_timecode(end_timecode, compensation_frames, offset_hours, rate)

            # Generate a unique ID for each clip
            clip_id = f"{i+1:04}"
            reel = f"{clip['camera']:02d}"
            edl_file.write(f"{clip_id}  {reel:<5} V     C   {start_timecode} {end_timecode} {start_timecode} {end_timecode}\n")
            edl_file.write(f"* FROM CLIP NAME: {edl_clip_name(clip['src'], clip['camera'])}\n")
            edl_file.write(f"* ATEM LOGGER SOURCE NAME: {' '.join(clip['src'].split())}\n")

    log.info(f"EDL file successfully generated: {file_path}")


EDL_EVENT = re.compile(r"^\s*\d+\s+\S+\s+\S+\s+\S+\s+"
                       r"(\d{2}:\d{2}:\d{2}[:;]\d{2})\s+"
                       r"(\d{2}:\d{2}:\d{2}[:;]\d{2})\s+"
                       r"(\d{2}:\d{2}:\d{2}[:;]\d{2})\s+"
                       r"(\d{2}:\d{2}:\d{2}[:;]\d{2})")


def read_edl(file_path):
    """Read cuts from a CMX-style EDL, using record in/out for the log."""
    clips = []
    drop_frame = False
    rate_hint = None
    with open(file_path, 'r', encoding='utf-8-sig') as edl_file:
        for line in edl_file:
            if line.upper().startswith('FCM:'):
                drop_frame = 'DROP FRAME' in line.upper() and 'NON-DROP' not in line.upper()
            if line.upper().startswith('* ATEM LOGGER TIMECODE RATE:'):
                saved_rate = line.split(':', 1)[1].strip()
                if saved_rate in TIMECODE_RATES:
                    rate_hint = saved_rate
            match = EDL_EVENT.match(line)
            if match:
                clip = {'src': f"Event {len(clips) + 1}",
                        'start': match.group(3), 'end': match.group(4)}
                reel = line.split()[1]
                if reel.isdigit() and valid_camera_number(int(reel)):
                    clip['camera'] = int(reel)
                clips.append(clip)
            elif line.lstrip().upper().startswith('* FROM CLIP NAME:') and clips:
                clips[-1]['src'] = line.split(':', 1)[1].strip()
                if 'camera' not in clips[-1]:
                    clips[-1]['camera'] = suggested_camera_number(clips[-1]['src'])
            elif line.lstrip().upper().startswith('* ATEM LOGGER SOURCE NAME:') and clips:
                clips[-1]['src'] = line.split(':', 1)[1].strip()
    if not clips:
        raise ValueError('No supported cut events were found in the EDL.')
    if rate_hint is None and drop_frame:
        highest_frame = max(int(clip[field][-2:]) for clip in clips for field in ('start', 'end'))
        rate_hint = "59.94 DF" if highest_frame >= 30 else "29.97 DF"
    return clips, rate_hint


def write_session(file_path, session):
    """Save raw cuts without requiring valid timecodes or an exportable EDL."""
    destination = Path(file_path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=destination.parent,
                                         prefix=f'.{destination.name}.', suffix='.tmp',
                                         delete=False) as session_file:
            temporary = Path(session_file.name)
            json.dump(session, session_file, ensure_ascii=False, indent=2)
            session_file.write('\n')
            session_file.flush()
            os.fsync(session_file.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_session(file_path):
    with open(file_path, 'r', encoding='utf-8') as session_file:
        session = json.load(session_file)
    if not isinstance(session, dict) or session.get('format') != 'ATEMLoggerSession' or session.get('version') != 1:
        raise ValueError('This is not a supported ATEM-EDL-TOOL session file.')
    if session.get('timecode_rate') not in TIMECODE_RATES:
        raise ValueError('The session has an unknown timecode frame rate.')
    clips = session.get('clips')
    if not isinstance(clips, list) or any(
        not isinstance(clip, dict) or any(not isinstance(clip.get(key), str)
                                          for key in ('src', 'start', 'end'))
        or (clip.get('camera') is not None and type(clip['camera']) not in (int, str))
        for clip in clips
    ):
        raise ValueError('The session contains invalid clip data.')
    for key, default in (('compensation_enabled', False), ('offset_enabled', False)):
        if not isinstance(session.get(key, default), bool):
            raise ValueError(f'The session has an invalid {key} setting.')
    for key, default in (('compensation_frames', 0), ('offset_hours', 0)):
        if type(session.get(key, default)) is not int:
            raise ValueError(f'The session has an invalid {key} setting.')
    for key in ('detected_video_format', 'last_display_timecode', 'last_rejected_timecode'):
        if not isinstance(session.get(key, ''), str):
            raise ValueError(f'The session has an invalid {key} value.')
    return session


def generate_csv(clips, file_path, compensation_frames=0, offset_hours=0, rate="24"):
    """Write the same source and timecode data as the EDL in spreadsheet form."""
    with open(file_path, 'w', newline='', encoding='utf-8-sig') as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(['Source', 'Camera #', 'Start', 'End', 'Duration'])
        for clip in clips:
            start = clip['start']
            end = clip['end']
            writer.writerow([clip['src'], clip.get('camera', ''), export_timecode(start, compensation_frames, offset_hours, rate),
                             export_timecode(end, compensation_frames, offset_hours, rate), duration_timecode(start, end, rate)])
    log.info(f"CSV file successfully generated: {file_path}")


def adjust_timecode(timecode, compensation_frames, rate="24"):
    return export_timecode(timecode, compensation_frames, rate=rate)

class HyperDeckConnection:
    def __init__(self, socket_conn):
        self.socket = socket_conn
        self.buffer = b''
        self.video_format = None

    def sendall(self, data):
        self.socket.sendall(data)

    def read_response(self):
        """HyperDeck messages end with a blank line, not the first newline."""
        while True:
            end = re.search(rb'\r?\n\r?\n', self.buffer)
            if end:
                message = self.buffer[:end.start()].decode('utf-8', errors='replace')
                self.buffer = self.buffer[end.end():]
                return message
            chunk = self.socket.recv(4096)
            if not chunk:
                raise ConnectionError("HyperDeck closed the connection")
            self.buffer += chunk

    def close(self):
        self.socket.close()

    def shutdown(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def connect_to_hyperdeck(ip, port=9993):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((ip, port))
        connection = HyperDeckConnection(s)
        connection.read_response()  # Initial 500 connection info message
        return connection
    except Exception as e:
        log.error(f"Error connecting to HyperDeck: {e}")
        return None

def get_timecode_from_hyperdeck(socket_conn, raise_errors=False):
    """
    Sends the "transport info" command and retrieves the "Display Timecode".
    """
    try:
        # Send the "transport info" command
        socket_conn.sendall(b"transport info\n")
        
        response_str = socket_conn.read_response()

        # The same transport response also reports the deck's video format.
        for line in response_str.split("\n"):
            if line.strip().lower().startswith("video format:"):
                socket_conn.video_format = line.split(":", 1)[1].strip()
        for line in response_str.split("\n"):
            if line.strip().lower().startswith("display timecode:"):
                return line.split(":", 1)[1].strip()

        # If no display timecode is found, display an error message
        log.debug("HyperDeck response contained no display timecode: %s", response_str)

    except Exception as e:
        log.error(f"Error retrieving timecode: {e}")
        if raise_errors:
            raise
    
    return None


def rate_from_video_format(video_format):
    """Map HyperDeck transport video format to a frame rate, when known."""
    if not video_format:
        return None
    match = re.search(r"([pi])(23976|23\.98|24|25|2997|29\.97|30|47952|47\.95|48|50|5994|59\.94|60)$",
                      video_format.strip(), re.IGNORECASE)
    if not match:
        return None
    scan, code = match.group(1).lower(), match.group(2)
    rate = {
        '23976': '23.976', '23.98': '23.976', '24': '24', '25': '25',
        '2997': '29.97', '29.97': '29.97', '30': '30',
        '47952': '47.952', '47.95': '47.952', '48': '48',
        '50': '50', '5994': '59.94', '59.94': '59.94', '60': '60',
    }[code]
    if scan == 'i':
        return {'50': '25', '59.94': '29.97', '60': '30'}.get(rate)
    return rate


class HyperDeckPreviewThread(QThread):
    """Keep the live HyperDeck readout moving while cut logging is idle."""
    update_timecode_signal = pyqtSignal(str)
    update_video_format_signal = pyqtSignal(str)

    def __init__(self, ip):
        super().__init__()
        self.ip = ip
        self.stop_event = threading.Event()
        self.hyperdeck_conn = None

    def request_stop(self):
        self.stop_event.set()
        if self.hyperdeck_conn:
            self.hyperdeck_conn.shutdown()

    def run(self):
        previous_video_format = None
        while not self.stop_event.is_set():
            if self.hyperdeck_conn is None:
                self.hyperdeck_conn = connect_to_hyperdeck(self.ip)
                if not self.hyperdeck_conn:
                    self.stop_event.wait(2)
                    continue
            try:
                timecode = get_timecode_from_hyperdeck(self.hyperdeck_conn, raise_errors=True)
                video_format = self.hyperdeck_conn.video_format
                if video_format != previous_video_format:
                    self.update_video_format_signal.emit(video_format or "")
                    previous_video_format = video_format
                if timecode:
                    self.update_timecode_signal.emit(timecode)
                self.stop_event.wait(0.1)
            except Exception as error:
                log.warning("HyperDeck preview connection lost: %s", error)
                try:
                    self.hyperdeck_conn.close()
                except (AttributeError, OSError):
                    pass
                self.hyperdeck_conn = None
        if self.hyperdeck_conn:
            try:
                self.hyperdeck_conn.close()
            except OSError:
                pass
            self.hyperdeck_conn = None


class ATEMMonitorThread(QThread):
    update_input_signal = pyqtSignal(str, str)
    update_log_signal = pyqtSignal(str, str, str, int)
    update_end_signal = pyqtSignal(str)
    update_timecode_signal = pyqtSignal(str)
    update_video_format_signal = pyqtSignal(str)
    rejected_timecode_signal = pyqtSignal(str)
    recording_started_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, atem_ip, hyperdeck_ip, stop_event, compensation_frames,
                 auto_start=False, timecode_rate="24"):
        super().__init__()
        self.atem_ip = atem_ip
        self.hyperdeck_ip = hyperdeck_ip
        self.stop_event = stop_event
        self.compensation_frames = compensation_frames
        self.auto_start = auto_start
        self.timecode_rate = timecode_rate
        self.hyperdeck_conn = None
        self.override_names = {}
        self.camera_numbers = {}
        self.override_lock = threading.Lock()

    def set_override(self, input_id, name):
        with self.override_lock:
            if name:
                self.override_names[input_id] = name
            else:
                self.override_names.pop(input_id, None)

    def set_camera_number(self, input_id, number):
        with self.override_lock:
            self.camera_numbers[input_id] = number

    def request_stop(self):
        self.stop_event.set()
        if self.hyperdeck_conn:
            self.hyperdeck_conn.shutdown()

    def run(self):
        atem = ATEMMax()
        try:
            connect_switcher(atem, self.atem_ip)
        except Exception as e:
            log.error(f"Error connecting to ATEM: {e}")
            self.error_signal.emit(f"ATEM connection failed: {e}")
            self.stop_event.set()
            return

        self.hyperdeck_conn = connect_to_hyperdeck(self.hyperdeck_ip)
        if not self.hyperdeck_conn:
            log.error(f"Unable to connect to HyperDeck {self.hyperdeck_ip}.")
            self.error_signal.emit(f"Unable to connect to HyperDeck {self.hyperdeck_ip}.")
            self.stop_event.set()
            atem.disconnect()
            return

        last_program_input = None
        last_program_name = None
        last_timecode = None
        latest_timecode = None
        run_detector = TimecodeRunDetector(self.timecode_rate)
        previous_video_format = None
        recording_started = not self.auto_start
        try:
            while not self.stop_event.is_set():
                try:
                    program_input = atem.programInput[atem.atem.mixEffects.mixEffect1].videoSource
                    input_id = str(source_id(program_input))
                    with self.override_lock:
                        override = self.override_names.get(input_id)
                        camera_number = self.camera_numbers.get(input_id, 0)
                    program_input_str = override or source_name(atem, program_input)

                    self.update_input_signal.emit(input_id, program_input_str)

                    timecode = get_timecode_from_hyperdeck(self.hyperdeck_conn)
                    video_format = self.hyperdeck_conn.video_format
                    if video_format != previous_video_format:
                        self.update_video_format_signal.emit(video_format or "")
                        previous_video_format = video_format
                    if timecode:
                        self.update_timecode_signal.emit(timecode)
                        try:
                            current_frames = timecode_to_frames(timecode, self.timecode_rate)
                        except ValueError as e:
                            log.warning("Ignoring invalid HyperDeck timecode %r: %s", timecode, e)
                            self.rejected_timecode_signal.emit(timecode)
                            run_detector.reset()
                            time.sleep(0.1)
                            continue
                        latest_timecode = timecode

                    if not recording_started:
                        if timecode and run_detector.observe(current_frames, time.monotonic()):
                            recording_started = True
                            self.recording_started_signal.emit()
                        else:
                            if not timecode:
                                run_detector.reset()
                            time.sleep(0.1)
                            continue

                    if not timecode:
                        time.sleep(0.1)
                        continue

                    if source_id(program_input) != source_id(last_program_input):
                        if last_program_input is not None and last_timecode:
                            self.update_end_signal.emit(timecode)
                        last_program_input = program_input
                        last_program_name = program_input_str
                        last_timecode = timecode
                        self.update_log_signal.emit(last_program_name, timecode, timecode, camera_number)
                        log.info(f"Program input at {timecode}: {program_input_str}")
                    else:
                        # Apply label edits to the clip currently on program, too.
                        last_program_name = program_input_str
                        self.update_end_signal.emit(timecode)
                        time.sleep(0.01)
                except Exception as e:
                    log.error(f"Error retrieving program input: {e}")

            if last_program_input is not None and last_timecode and latest_timecode:
                self.update_end_signal.emit(latest_timecode)

        except KeyboardInterrupt:
            pass
        finally:
            # Ensure proper disconnection
            atem.disconnect()
            if self.hyperdeck_conn:
                self.hyperdeck_conn.close()

class RetroTimecodeDisplay(QLCDNumber):
    """Seven-segment readout styled after a vintage deck timecode display."""
    def __init__(self, text="00:00:00:00", parent=None):
        super().__init__(11, parent)
        self._text = text
        self.setSegmentStyle(QLCDNumber.SegmentStyle.Filled)
        self.setAccessibleName(text)
        self.setAccessibleDescription("Current HyperDeck timecode")
        self.display(text)

    def setText(self, text):
        self._text = str(text)
        self.setAccessibleName(self._text)
        self.display(self._text)

    def text(self):
        return self._text


class TimelineRuler(QWidget):
    """Paint a timecode ruler aligned with the horizontal shot track."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.start_frame = 0
        self.total_frames = 0
        self.rate = "24"
        self.pixels_per_second = 64
        self.setFixedHeight(34)

    def configure(self, start_frame, total_frames, rate, pixels_per_second, width):
        self.start_frame = start_frame
        self.total_frames = total_frames
        self.rate = rate
        self.pixels_per_second = pixels_per_second
        self.setFixedWidth(max(1, width))
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#141719"))
        painter.setFont(self.font())
        fps = TIMECODE_RATES[self.rate][0]
        intervals = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600)
        interval = next((seconds for seconds in intervals
                         if seconds * self.pixels_per_second >= 118), intervals[-1])
        interval_frames = max(1, interval * fps)
        painter.setPen(QPen(QColor("#00dce8"), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.setPen(QPen(QColor("#7f8a90"), 1))
        offset = 0
        while offset <= self.total_frames:
            x = round(offset / max(1, fps) * self.pixels_per_second)
            painter.drawLine(x, 18, x, self.height() - 1)
            tc = frames_to_timecode((self.start_frame + offset) % frames_per_day(self.rate), self.rate)
            painter.setPen(QColor("#b8c0c5"))
            painter.drawText(x + 5, 15, tc)
            painter.setPen(QPen(QColor("#7f8a90"), 1))
            offset += interval_frames
        painter.end()


class ATEMGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("TelioTortay", "ATEM Logger")
        self.setWindowTitle(f"ATEM-EDL-TOOL v{APP_VERSION}")
        self.setGeometry(100, 100, 1200, 800)
        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(16, 14, 16, 16)
        self.layout.setSpacing(12)

        self.input_groupbox = QGroupBox("Video Inputs")
        self.input_layout = QVBoxLayout()

        # Timecode box with green border by default
        self.timecode_frame = QFrame()
        self.timecode_frame.setObjectName("timecodeFrame")
        self.timecode_frame_layout = QVBoxLayout()
        self.timecode_frame_layout.setContentsMargins(14, 14, 14, 14)
        self.timecode_display = RetroTimecodeDisplay("00:00:00:00")
        self.timecode_display.setObjectName("timecodeDisplay")
        self.timecode_display.setMinimumWidth(330)
        self.timecode_display.setFixedHeight(70)
        self.detected_fps_label = QLabel("FPS: —")
        self.detected_fps_label.setObjectName("detectedFpsLabel")
        self.detected_fps_label.setFixedWidth(116)
        self.detected_fps_label.setFixedHeight(70)
        self.detected_fps_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detected_fps_label.setToolTip("Frame rate reported by the HyperDeck video format")
        timecode_line = QHBoxLayout()
        timecode_line.setSpacing(12)
        timecode_line.addWidget(self.timecode_display, 1)
        timecode_line.addWidget(self.detected_fps_label)
        self.timecode_frame_layout.addLayout(timecode_line)
        self.timecode_frame.setLayout(self.timecode_frame_layout)
        self.set_timecode_color("#7c838a")
        self.timecode_warning = QLabel("")
        self.timecode_warning.setStyleSheet("color: #c47f00; font-weight: bold;")
        self.timecode_warning.setWordWrap(True)
        self.input_layout.addWidget(self.timecode_warning)

        self.input_list_label = QLabel("Available Inputs:")
        self.input_list_label.setStyleSheet("font-weight: bold;")
        self.input_layout.addWidget(self.input_list_label)

        self.input_list = QTableWidget(0, 5)
        self.input_list.setHorizontalHeaderLabels(["Input", "ATEM name", "Override", "Custom name", "CuePilot #"])
        self.input_list.horizontalHeader().setStretchLastSection(False)
        for column, width in enumerate((80, 90, 70, 130, 90)):
            self.input_list.setColumnWidth(column, width)
        self.input_layout.addWidget(self.input_list)
        self.input_rows = {}

        self.input_groupbox.setLayout(self.input_layout)

        self.right_layout = QVBoxLayout()

        self.ip_label = QLabel("ATEM IP:")
        self.ip_input = QLineEdit(str(self.settings.value("atem_ip", "")))
        self.ip_input.textChanged.connect(lambda value: self.settings.setValue("atem_ip", value))
        self.right_layout.addWidget(self.ip_label)
        self.right_layout.addWidget(self.ip_input)

        self.hyperdeck_ip_label = QLabel("HyperDeck IP:")
        self.hyperdeck_ip_input = QLineEdit(str(self.settings.value("hyperdeck_ip", "")))
        self.hyperdeck_ip_input.textChanged.connect(lambda value: self.settings.setValue("hyperdeck_ip", value))
        self.right_layout.addWidget(self.hyperdeck_ip_label)
        self.right_layout.addWidget(self.hyperdeck_ip_input)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_to_atem)
        self.connection_light = QLabel()
        self.set_connection_state("disconnected")

        self.frames_label = QLabel("Frame Compensation:")
        self.frames_input = QSpinBox()
        self.frames_input.setRange(-1000, 1000)
        self.frames_input.setValue(self.settings.value("compensation_frames", 2, type=int))
        self.frames_input.valueChanged.connect(lambda value: self.settings.setValue("compensation_frames", value))
        self.right_layout.addWidget(self.frames_label)
        self.right_layout.addWidget(self.frames_input)

        # Checkbox to enable/disable frame compensation
        self.compensation_checkbox = QCheckBox("Enable frame compensation")
        self.compensation_checkbox.setChecked(self.settings.value("compensation_enabled", False, type=bool))
        self.compensation_checkbox.toggled.connect(self.toggle_frames_input)
        self.compensation_checkbox.toggled.connect(lambda checked: self.settings.setValue("compensation_enabled", checked))
        self.frames_input.setEnabled(self.compensation_checkbox.isChecked())
        self.right_layout.addWidget(self.compensation_checkbox)

        self.fps_label = QLabel("Timecode frame rate:")
        self.fps_combo = QComboBox()
        for rate in TIMECODE_RATES:
            self.fps_combo.addItem(f"{rate} fps", rate)
        saved_rate = str(self.settings.value("timecode_rate", "24"))
        self.fps_combo.setCurrentIndex(max(0, self.fps_combo.findData(saved_rate)))
        self.fps_combo.currentIndexChanged.connect(self.frame_rate_changed)
        self.right_layout.addWidget(self.fps_label)
        self.right_layout.addWidget(self.fps_combo)

        self.auto_start_checkbox = QCheckBox("Auto start when HyperDeck timecode runs")
        self.auto_start_checkbox.setChecked(self.settings.value("auto_start", False, type=bool))
        self.auto_start_checkbox.toggled.connect(self.update_start_button)
        self.auto_start_checkbox.toggled.connect(lambda checked: self.settings.setValue("auto_start", checked))
        self.right_layout.addWidget(self.auto_start_checkbox)

        self.offset_checkbox = QCheckBox("Offset timecode on export")
        self.offset_checkbox.setChecked(self.settings.value("offset_enabled", False, type=bool))
        self.offset_checkbox.toggled.connect(lambda checked: self.settings.setValue("offset_enabled", checked))
        self.right_layout.addWidget(self.offset_checkbox)
        self.offset_hours = QSpinBox()
        self.offset_hours.setRange(-23, 23)
        self.offset_hours.setSuffix(" hours")
        self.offset_hours.setValue(self.settings.value("offset_hours", 0, type=int))
        self.offset_hours.valueChanged.connect(lambda value: self.settings.setValue("offset_hours", value))
        self.offset_hours.setEnabled(self.offset_checkbox.isChecked())
        self.offset_checkbox.toggled.connect(self.offset_hours.setEnabled)
        self.right_layout.addWidget(self.offset_hours)

        self.current_input_label = QLabel("Selected input: Not defined")
        self.right_layout.addWidget(self.current_input_label)

        self.start_button = QPushButton("Arm" if self.auto_start_checkbox.isChecked() else "Start")
        self.start_button.clicked.connect(self.toggle_monitoring)

        self.save_button = QPushButton("Save EDL + CSV now…")
        self.save_button.clicked.connect(self.choose_save_location)
        self.import_button = QPushButton("Import EDL…")
        self.import_button.clicked.connect(self.import_edl)
        self.open_session_button = QPushButton("Open Session…")
        self.open_session_button.clicked.connect(self.choose_open_session)
        self.save_session_button = QPushButton("Save Session…")
        self.save_session_button.clicked.connect(self.choose_save_session)
        self.save_status_label = QLabel("No export saved yet")
        self.log_issue_label = QLabel("No timecode errors")
        self.log_issue_label.setWordWrap(True)

        self.log_table = QTableWidget(0, 5)
        self.log_table.setHorizontalHeaderLabels(["Source", "Start", "End", "Duration", "Camera #"])
        self.log_table.horizontalHeader().setStretchLastSection(False)
        self.log_table.setColumnWidth(4, 90)
        self.log_table.itemChanged.connect(self.log_item_changed)
        self.log_table.itemSelectionChanged.connect(self.select_timeline_from_log)
        self.log_table.installEventFilter(self)
        self.log_table.viewport().installEventFilter(self)
        self.timeline_title = QLabel("SHOT SEQUENCE")
        self.timeline_title.setObjectName("sectionTitle")
        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setWidgetResizable(False)
        self.timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.timeline_scroll.setMinimumHeight(132)
        self.timeline_scroll.setMaximumHeight(150)
        self.timeline_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.timeline_content = QWidget()
        self.timeline_content_layout = QVBoxLayout(self.timeline_content)
        self.timeline_content_layout.setContentsMargins(0, 0, 0, 0)
        self.timeline_content_layout.setSpacing(0)
        self.timeline_ruler = TimelineRuler()
        self.timeline_ruler.installEventFilter(self)
        self.timeline_content_layout.addWidget(self.timeline_ruler)
        self.timeline_track = QWidget()
        self.timeline_track.installEventFilter(self)
        self.timeline_layout = QHBoxLayout(self.timeline_track)
        self.timeline_layout.setContentsMargins(0, 0, 0, 0)
        self.timeline_layout.setSpacing(0)
        self.timeline_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.timeline_content_layout.addWidget(self.timeline_track)
        self.timeline_buttons = []
        self.timeline_scroll_targets = []
        self.timeline_positions = []
        self.timeline_origin_frame = 0
        self.timeline_total_frames = 0
        self.timeline_pixels_per_second = 64
        self.timeline_scroll_animation = None
        self.timeline_content.installEventFilter(self)
        self.timeline_scroll.viewport().installEventFilter(self)
        self.suppress_timeline_selection = False
        self.timeline_scroll.setWidget(self.timeline_content)
        session_button_layout = QHBoxLayout()
        session_button_layout.addWidget(self.open_session_button)
        session_button_layout.addWidget(self.save_session_button)
        button_layout = QHBoxLayout()
        button_layout.addWidget(self.import_button)
        button_layout.addWidget(self.save_button)
        # Rebuild the window around an operator header, setup deck, and full-width
        # sequence area. The old layout buried the shot history in a narrow side column.
        self.right_layout.removeWidget(self.save_status_label)
        self.right_layout.removeWidget(self.log_issue_label)
        self.input_groupbox.setTitle("INPUT PATCH  /  CUEPILOT CAMERA MAP")
        self.input_groupbox.setObjectName("panel")
        self.input_groupbox.setMinimumHeight(250)
        self.input_groupbox.setMaximumHeight(470)

        header = QFrame()
        header.setObjectName("operatorHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)
        identity = QVBoxLayout()
        brand = QLabel("ATEM-EDL-TOOL")
        brand.setObjectName("brand")
        subtitle = QLabel("LIVE CUT LOG  /  CUEPILOT EDL")
        subtitle.setObjectName("subtitle")
        identity.addWidget(brand)
        identity.addWidget(subtitle)
        header_layout.addLayout(identity, 1)
        header_layout.addWidget(self.timecode_frame, 2)
        connection_box = QVBoxLayout()
        self.connection_light.setAlignment(Qt.AlignmentFlag.AlignCenter)
        connection_box.addWidget(self.connection_light)
        connection_actions = QHBoxLayout()
        self.connect_button.setObjectName("connectButton")
        self.start_button.setObjectName("startButton")
        connection_actions.addWidget(self.connect_button)
        connection_actions.addWidget(self.start_button)
        connection_box.addLayout(connection_actions)
        header_layout.addLayout(connection_box, 2)
        self.layout.addWidget(header)

        setup_row = QHBoxLayout()
        setup_row.setSpacing(12)
        setup_row.addWidget(self.input_groupbox, 6)
        settings_panel = QGroupBox("SWITCHER / TIMECODE SETUP")
        settings_panel.setObjectName("panel")
        settings_panel.setLayout(self.right_layout)
        settings_panel.setMaximumHeight(470)
        setup_row.addWidget(settings_panel, 5)
        self.layout.addLayout(setup_row)

        sequence_panel = QFrame()
        sequence_panel.setObjectName("sequencePanel")
        sequence_layout = QVBoxLayout(sequence_panel)
        sequence_layout.setContentsMargins(14, 10, 14, 12)
        sequence_heading = QHBoxLayout()
        sequence_heading.addWidget(self.timeline_title)
        sequence_heading.addStretch(1)
        sequence_heading.addWidget(self.save_status_label)
        sequence_layout.addLayout(sequence_heading)
        sequence_layout.addWidget(self.timeline_scroll)
        sequence_layout.addWidget(self.log_issue_label)
        log_heading = QLabel("CUT LOG  /  EDITABLE SOURCE + TIMECODE")
        log_heading.setObjectName("sectionTitle")
        sequence_layout.addWidget(log_heading)
        self.log_table.setMinimumHeight(180)
        sequence_layout.addWidget(self.log_table, 1)
        sequence_layout.addLayout(session_button_layout)
        sequence_layout.addLayout(button_layout)
        self.layout.addWidget(sequence_panel, 1)

        self.setStyleSheet("""
            QWidget { background: #181a1c; color: #d9dcdf; font-size: 12px; }
            QLabel, QCheckBox { background: transparent; }
            QFrame#operatorHeader { background: #0d0f10; border: 1px solid #33383c; border-radius: 7px; }
            QFrame#sequencePanel, QGroupBox#panel { background: #202326; border: 1px solid #393e42; border-radius: 7px; }
            QGroupBox { margin-top: 14px; padding: 12px 8px 8px; font-weight: bold; color: #c7cbd0; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #00dce8; }
            QLabel#brand { color: #f1f2f3; font-size: 20px; font-weight: bold; letter-spacing: 2px; }
            QLabel#subtitle, QLabel#sectionTitle { color: #00dce8; font-size: 11px; font-weight: bold; letter-spacing: 1px; }
            QLineEdit, QSpinBox, QComboBox { background: #101214; border: 1px solid #454a4e; border-radius: 4px; padding: 5px; selection-background-color: #006c76; }
            QPushButton { background: #303438; border: 1px solid #555b60; border-radius: 4px; padding: 7px 10px; font-weight: bold; }
            QPushButton:hover { background: #42474c; border-color: #00dce8; }
            QPushButton#connectButton { color: #00dce8; border-color: #007b83; }
            QPushButton#startButton { background: #303438; border-color: #555b60; color: #d9dcdf; min-width: 96px; }
            QPushButton:disabled { color: #777; background: #25282a; }
            QTableWidget { background: #111315; alternate-background-color: #1b1e20; gridline-color: #2e3337; border: 1px solid #3b4044; selection-background-color: #68401e; }
            QHeaderView::section { background: #303438; color: #e1e3e5; padding: 6px; border: 0; border-right: 1px solid #45494d; font-weight: bold; }
            QScrollBar:horizontal { background: #111315; height: 13px; margin: 0; }
            QScrollBar::handle:horizontal { background: #646a70; min-width: 28px; border-radius: 5px; }
        """)

        self.setLayout(self.layout)

        self.monitor_thread = None
        self.preview_thread = None
        self.file_path = str(self.settings.value("last_export_path", "")) or None
        self.session_file_path = str(self.settings.value("last_session_path", "")) or None
        self.is_monitoring = False
        self.recording_active = False
        self.atem = ATEMMax()
        self.stop_event = threading.Event()
        self.last_program_input = None
        self.atem_connected = False
        self.has_atem_session = False
        self.clips = []
        self.last_timeline_live_update = 0.0
        self.refresh_timeline()
        self.updating_log_table = False
        self.detected_rate = None
        self.detected_video_format = None
        self.last_rejected_timecode = ""
        self.close_pending = False
        self.monitor_shutdown_complete = True
        self.connection_timer = QTimer(self)
        self.connection_timer.timeout.connect(self.refresh_connection_state)
        self.connection_timer.start(1000)
        self.connect_timer = QTimer(self)
        self.connect_timer.timeout.connect(self.check_atem_connection)
        self.connection_deadline = None
        self.names_first_seen_at = None
        self.next_connection_attempt_at = None
        self.connection_attempt = 0
        self.start_button.setEnabled(False)
        saved_geometry = self.settings.value("window_geometry")
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)

    def connect_to_atem(self):
        if self.has_atem_session or self.atem_connected:
            self.disconnect_from_atem()
            return
        atem_ip = self.ip_input.text()
        if not self.is_valid_ip(atem_ip):
            self.show_error("Invalid IP Address", "The entered IP address is incorrect or unreachable. Please verify the IP and try again.")
            return

        hyperdeck_ip = self.hyperdeck_ip_input.text()
        if not self.is_valid_ip(hyperdeck_ip):
            self.show_error("Invalid HyperDeck IP Address", "The HyperDeck IP address is incorrect. Please verify the IP and try again.")
            return

        try:
            self.set_connection_state("connecting")
            self.connect_button.setEnabled(False)
            self.start_button.setEnabled(False)
            self.atem_connected = False
            self.has_atem_session = False
            self.connection_deadline = time.monotonic() + 30
            self.names_first_seen_at = None
            self.connection_attempt = 0
            self.restart_atem_connection()
            self.connect_timer.start(100)
        except Exception as e:
            self.finish_connection_error(e)

    def restart_atem_connection(self):
        self.atem.disconnect()
        self.atem = ATEMMax()
        self.atem.connect(self.ip_input.text())
        self.connection_attempt += 1
        self.next_connection_attempt_at = time.monotonic() + 6
        self.names_first_seen_at = None
        self.set_connection_state("connecting", f" (attempt {self.connection_attempt})")

    def check_atem_connection(self):
        now = time.monotonic()
        try:
            if self.atem.switcherAlive and switcher_has_input_names(self.atem):
                if self.names_first_seen_at is None:
                    self.names_first_seen_at = now
                if now - self.names_first_seen_at >= 0.5:
                    self.connect_timer.stop()
                    self.populate_input_list()
                    self.atem_connected = True
                    self.has_atem_session = True
                    self.connect_button.setEnabled(True)
                    self.start_button.setEnabled(True)
                    self.set_connection_state("connected")
                    self.start_hyperdeck_preview()
                    return
            if now >= self.connection_deadline:
                raise ConnectionError("ATEM did not respond after several fresh connection attempts. Check System Settings → Privacy & Security → Local Network, then try Connect again.")
            if self.names_first_seen_at is None and now >= self.next_connection_attempt_at:
                self.restart_atem_connection()
        except Exception as e:
            self.finish_connection_error(e)

    def finish_connection_error(self, error):
        self.connect_timer.stop()
        self.atem_connected = False
        self.has_atem_session = False
        self.connect_button.setEnabled(True)
        self.start_button.setEnabled(False)
        self.set_connection_state("disconnected")
        self.atem.disconnect()
        self.show_error("Connection Error", str(error))

    def disconnect_from_atem(self):
        self.connect_timer.stop()
        self.atem_connected = False
        self.has_atem_session = False
        if self.preview_thread is not None and self.preview_thread.isRunning():
            self.preview_thread.request_stop()
            self.preview_thread.wait(1500)
        self.atem.disconnect()
        self.set_connection_state("disconnected")
        self.start_button.setEnabled(False)

    def populate_input_list(self):
        self.input_list.setRowCount(0)
        self.input_rows.clear()
        atem_ip = self.ip_input.text()
        for number in range(1, 41):
            source = getattr(self.atem.atem.videoSources, f"input{number}")
            try:
                properties = self.atem.inputProperties[source]
                if not (properties.longName or properties.shortName):
                    continue
            except (KeyError, AttributeError, TypeError):
                continue
            input_id = str(source_id(source))
            row = self.input_list.rowCount()
            self.input_list.insertRow(row)
            source_item = QTableWidgetItem(str(source))
            name_item = QTableWidgetItem(source_name(self.atem, source))
            source_item.setFlags(source_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.input_list.setItem(row, 0, source_item)
            self.input_list.setItem(row, 1, name_item)
            checkbox = QCheckBox()
            name_edit = QLineEdit()
            name_edit.setPlaceholderText("Custom name")
            setting_key = f"overrides/{atem_ip}/{input_id}"
            name_edit.setText(str(self.settings.value(f"{setting_key}/name", "")))
            checkbox.setChecked(self.settings.value(f"{setting_key}/enabled", False, type=bool))
            name_edit.setEnabled(checkbox.isChecked())
            checkbox.toggled.connect(name_edit.setEnabled)
            checkbox.toggled.connect(lambda checked, key=input_id: self.update_override(key))
            name_edit.textChanged.connect(lambda text, key=input_id: self.update_override(key))
            self.input_list.setCellWidget(row, 2, checkbox)
            self.input_list.setCellWidget(row, 3, name_edit)
            camera_spin = QSpinBox()
            camera_spin.setRange(0, 48)
            camera_spin.setSpecialValueText("Unmapped")
            camera_spin.setValue(self.settings.value(f"camera_numbers/{atem_ip}/{input_id}", number, type=int))
            camera_spin.valueChanged.connect(lambda value, key=input_id: self.update_camera_number(key))
            self.input_list.setCellWidget(row, 4, camera_spin)
            self.input_rows[input_id] = (row, checkbox, name_edit, camera_spin)

    def set_connection_state(self, state, detail=""):
        color, label = {
            "disconnected": ("#d32f2f", "Disconnected"),
            "connecting": ("#d69e00", "Connecting…"),
            "connected": ("#239b45", "Connected"),
        }[state]
        self.connection_light.setText(f"● ATEM {label}{detail}")
        self.connection_light.setStyleSheet(f"color: {color}; font-weight: bold;")
        self.connect_button.setText({"disconnected": "Connect", "connecting": "Connecting…",
                                     "connected": "Disconnect"}[state])
        if getattr(self, "is_monitoring", False) and self.recording_active:
            readout_color = "#ff3434"
        elif getattr(self, "is_monitoring", False) and state == "connected":
            readout_color = "#ffbd38"
        else:
            readout_color = "#239b45" if state == "connected" else "#7c838a"
        self.set_timecode_color(readout_color)

    def refresh_connection_state(self):
        if self.has_atem_session:
            alive = bool(self.atem.switcherAlive)
            if alive != self.atem_connected:
                self.atem_connected = alive
                self.set_connection_state("connected" if alive else "disconnected")
                self.connect_button.setEnabled(True)
                if not self.is_monitoring:
                    self.start_button.setEnabled(alive)
                if not alive:
                    self.has_atem_session = False
                    if self.preview_thread is not None and self.preview_thread.isRunning():
                        self.preview_thread.request_stop()

    def update_start_button(self, _enabled=None):
        if not self.is_monitoring:
            self.start_button.setText("Arm" if self.auto_start_checkbox.isChecked() else "Start")
            self.start_button.setStyleSheet("")

    def start_hyperdeck_preview(self):
        if not self.atem_connected:
            return
        if self.preview_thread is not None and self.preview_thread.isRunning():
            return
        self.preview_thread = HyperDeckPreviewThread(self.hyperdeck_ip_input.text())
        self.preview_thread.update_timecode_signal.connect(self.update_timecode)
        self.preview_thread.update_video_format_signal.connect(self.update_video_format)
        self.preview_thread.finished.connect(self.on_hyperdeck_preview_finished)
        self.preview_thread.start()

    def on_hyperdeck_preview_finished(self):
        if self.atem_connected:
            QTimer.singleShot(0, self.start_hyperdeck_preview)

    def recording_started(self):
        if not self.is_monitoring or self.sender() is not self.monitor_thread:
            return
        self.recording_active = True
        self.start_button.setText("Stop")
        self.start_button.setStyleSheet("background-color: #c62828; color: white; font-weight: bold;")
        self.set_timecode_color("#ff3434")

    def monitoring_error(self, message):
        if self.sender() is not self.monitor_thread:
            return
        self.monitor_thread.request_stop()
        if not self.close_pending:
            self.show_error("Monitoring Error", message)

    def monitoring_finished(self):
        if self.sender() is not self.monitor_thread:
            return
        self.is_monitoring = False
        self.monitor_shutdown_complete = True
        self.recording_active = False
        self.auto_start_checkbox.setEnabled(True)
        self.connect_button.setEnabled(True)
        self.start_button.setEnabled(self.atem_connected)
        self.set_logging_controls_locked(False)
        self.update_start_button()
        self.set_timecode_color("#239b45" if self.atem_connected else "#7c838a")
        self.start_hyperdeck_preview()
        if self.close_pending:
            self.close()

    def set_timecode_color(self, color):
        palette = self.timecode_display.palette()
        palette.setColor(QPalette.ColorRole.WindowText, QColor(color))
        palette.setColor(QPalette.ColorRole.Light, QColor(color))
        palette.setColor(QPalette.ColorRole.Dark, QColor(color).darker(220))
        self.timecode_display.setPalette(palette)
        self.detected_fps_label.setStyleSheet(
            f"color: {color}; background: transparent; font-family: Monaco, monospace; "
            "font-size: 17px; font-weight: bold; letter-spacing: 1px;"
        )
        self.timecode_frame.setStyleSheet("""
            QFrame#timecodeFrame { border: none; border-radius: 0; background-color: #09080d; }
            QLCDNumber#timecodeDisplay { color: %s; border: none; background-color: #09080d; }
        """ % color)

    def toggle_frames_input(self):
        """
        Enables or disables the frame compensation input field
        based on the checkbox state.
        """
        if self.compensation_checkbox.isChecked():
            self.frames_input.setEnabled(True)
        else:
            self.frames_input.setEnabled(False)

    def set_logging_controls_locked(self, locked):
        for widget in (self.ip_input, self.hyperdeck_ip_input, self.connect_button,
                       self.compensation_checkbox, self.auto_start_checkbox,
                       self.offset_checkbox, self.fps_combo, self.input_list,
                       self.save_button, self.import_button,
                       self.save_session_button, self.open_session_button,
                       self.log_table):
            widget.setEnabled(not locked)
        self.frames_input.setEnabled(not locked and self.compensation_checkbox.isChecked())
        self.offset_hours.setEnabled(not locked and self.offset_checkbox.isChecked())

    def toggle_monitoring(self):
        if self.is_monitoring:
            self.start_button.setText("Stopping…")
            self.start_button.setEnabled(False)
            self.monitor_thread.request_stop()
        else:
            if not self.is_valid_ip(self.ip_input.text()) or not self.is_valid_ip(self.hyperdeck_ip_input.text()):
                self.show_error("Invalid IP Address", "Enter valid ATEM and HyperDeck IP addresses first.")
                return
            self.clips = []
            self.log_table.setRowCount(0)
            self.refresh_timeline()
            self.refresh_log_issue_summary()
            self.save_status_label.setText("No export saved yet")
            self.timecode_warning.setText("")
            self.last_rejected_timecode = ""
            self.update_video_format("")
            self.stop_event.clear()
            self.monitor_shutdown_complete = False
            compensation_frames = self.frames_input.value() if self.compensation_checkbox.isChecked() else 0
            self.monitor_thread = ATEMMonitorThread(self.ip_input.text(),
                                                    self.hyperdeck_ip_input.text(),
                                                    self.stop_event,
                                                    compensation_frames,
                                                    self.auto_start_checkbox.isChecked(),
                                                    self.fps_combo.currentData())
            for input_id in self.input_rows:
                self.update_override(input_id)
                self.update_camera_number(input_id)
            self.monitor_thread.update_input_signal.connect(self.update_current_input)
            self.monitor_thread.update_log_signal.connect(self.update_log_table)
            self.monitor_thread.update_end_signal.connect(self.update_live_end)
            self.monitor_thread.update_timecode_signal.connect(self.update_timecode)
            self.monitor_thread.update_video_format_signal.connect(self.update_video_format)
            self.monitor_thread.rejected_timecode_signal.connect(self.show_rejected_timecode)
            self.monitor_thread.recording_started_signal.connect(self.recording_started)
            self.monitor_thread.error_signal.connect(self.monitoring_error)
            self.monitor_thread.finished.connect(self.monitoring_finished)
            self.monitor_thread.start()
            self.is_monitoring = True
            self.recording_active = not self.auto_start_checkbox.isChecked()
            self.set_logging_controls_locked(True)
            self.start_button.setText("Disarm" if self.auto_start_checkbox.isChecked() else "Stop")
            self.start_button.setStyleSheet("" if self.auto_start_checkbox.isChecked() else
                                            "background-color: #c62828; color: white; font-weight: bold;")
            self.set_timecode_color("#ffbd38" if self.auto_start_checkbox.isChecked() else "#ff3434")

    def choose_save_location(self):
        if self.is_monitoring:
            return
        file_path, _ = QFileDialog.getSaveFileName(self, "Save EDL and CSV", self.file_path or "", "EDL Files (*.edl)")
        if file_path:
            self.file_path = str(Path(file_path).with_suffix('.edl'))
            self.settings.setValue("last_export_path", self.file_path)
            self.save_outputs()

    def import_edl(self):
        if self.is_monitoring:
            return
        file_path, _ = QFileDialog.getOpenFileName(self, "Import EDL", "", "EDL Files (*.edl);;All Files (*)")
        if not file_path:
            return
        try:
            clips, rate_hint = read_edl(file_path)
            self.clips = clips
            self.log_table.setRowCount(0)
            self.last_rejected_timecode = ""
            if rate_hint:
                self.fps_combo.setCurrentIndex(self.fps_combo.findData(rate_hint))
            for clip in clips:
                self.add_log_row(clip)
            self.refresh_timeline()
            # Imported record timecodes already include any export adjustment.
            self.compensation_checkbox.setChecked(False)
            self.offset_checkbox.setChecked(False)
            self.refresh_durations()
            self.refresh_timecode_warning()
            rate_note = "" if rate_hint else " The EDL does not specify FPS; select it before saving."
            self.save_status_label.setText(f"Imported {len(clips)} cuts. Edit cells before saving.{rate_note}")
        except (OSError, UnicodeError, ValueError) as e:
            self.show_error("Import Error", str(e))

    def choose_save_session(self):
        if self.is_monitoring:
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save recovery session", self.session_file_path or "",
            "ATEM-EDL-TOOL Sessions (*.atemlog)")
        if file_path:
            try:
                self.save_session_to_path(str(Path(file_path).with_suffix('.atemlog')))
            except (OSError, ValueError) as e:
                self.save_status_label.setText(f"Session save failed: {e}")
                self.show_error("Session Save Error", str(e))

    def save_session_to_path(self, file_path):
        if self.is_monitoring:
            raise ValueError('Stop logging before saving a session.')
        session = {
            'format': 'ATEMLoggerSession', 'version': 1,
            'timecode_rate': self.fps_combo.currentData(),
            'detected_video_format': self.detected_video_format or '',
            'last_display_timecode': self.timecode_display.text(),
            'last_rejected_timecode': self.last_rejected_timecode,
            'compensation_enabled': self.compensation_checkbox.isChecked(),
            'compensation_frames': self.frames_input.value(),
            'offset_enabled': self.offset_checkbox.isChecked(),
            'offset_hours': self.offset_hours.value(),
            'clips': [clip.copy() for clip in self.clips],
        }
        write_session(file_path, session)
        self.session_file_path = file_path
        self.settings.setValue('last_session_path', file_path)
        self.save_status_label.setText(f"Session saved: {file_path}")

    def choose_open_session(self):
        if self.is_monitoring:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open recovery session", self.session_file_path or "",
            "ATEM-EDL-TOOL Sessions (*.atemlog);;All Files (*)")
        if file_path:
            try:
                self.open_session_from_path(file_path)
            except (OSError, UnicodeError, ValueError) as e:
                self.show_error("Session Open Error", str(e))

    def open_session_from_path(self, file_path):
        if self.is_monitoring:
            raise ValueError('Stop logging before opening a session.')
        session = read_session(file_path)
        self.clips = [{'src': clip['src'], 'start': clip['start'], 'end': clip['end'],
                       'camera': clip.get('camera')}
                      for clip in session['clips']]
        self.log_table.setRowCount(0)
        self.fps_combo.setCurrentIndex(self.fps_combo.findData(session['timecode_rate']))
        self.frames_input.setValue(session.get('compensation_frames', 0))
        self.compensation_checkbox.setChecked(session.get('compensation_enabled', False))
        self.offset_hours.setValue(session.get('offset_hours', 0))
        self.offset_checkbox.setChecked(session.get('offset_enabled', False))
        for clip in self.clips:
            self.add_log_row(clip)
        self.refresh_timeline()
        self.update_timecode(session.get('last_display_timecode', '00:00:00:00'))
        self.last_rejected_timecode = session.get('last_rejected_timecode', '')
        self.update_video_format(session.get('detected_video_format', ''))
        if self.detected_rate:
            self.detected_fps_label.setText(f"FPS: {self.detected_rate}")
            self.detected_fps_label.setToolTip('Video format recorded in this saved session')
        self.refresh_log_issue_summary()
        self.session_file_path = file_path
        self.settings.setValue('last_session_path', file_path)
        self.save_status_label.setText(f"Opened {len(self.clips)} cuts from {file_path}")

    def save_outputs(self):
        if not self.file_path:
            return
        try:
            offset = self.offset_hours.value() if self.offset_checkbox.isChecked() else 0
            rate = self.fps_combo.currentData()
            if not self.clips:
                raise ValueError("There are no cuts to export.")
            for row, clip in enumerate(self.clips, 1):
                for field in ('start', 'end'):
                    try:
                        timecode_to_frames(clip[field], rate)
                    except ValueError as e:
                        raise ValueError(f"Row {row} {field}: {e}") from e
            generate_edl(self.clips, self.file_path, self.current_compensation_frames(), offset, rate)
            generate_csv(self.clips, str(Path(self.file_path).with_suffix('.csv')), self.current_compensation_frames(), offset, rate)
            self.save_status_label.setText(f"Saved {len(self.clips)} cuts: {self.file_path}")
        except (OSError, ValueError) as e:
            guidance = "\n\nUse Save Session… to preserve the raw log, then reopen it to correct the highlighted timecodes." if isinstance(e, ValueError) else ""
            self.save_status_label.setText(f"Save failed: {e}")
            self.show_error("Save Error", f"{e}{guidance}")

    def current_compensation_frames(self):
        return self.frames_input.value() if self.compensation_checkbox.isChecked() else 0

    def update_current_input(self, input_id, input_str):
        self.current_input_label.setText(f"Selected input: {input_str}")

        for key, (row, _, _, _) in self.input_rows.items():
            for column in (0, 1):
                item = self.input_list.item(row, column)
                item.setBackground(QBrush(QColor(220, 45, 45)) if key == input_id else QBrush())
                item.setForeground(QBrush(QColor("white")) if key == input_id else QBrush())

    def update_override(self, input_id):
        if input_id not in self.input_rows:
            return
        _, checkbox, name_edit, _ = self.input_rows[input_id]
        name = name_edit.text().strip() if checkbox.isChecked() else ""
        setting_key = f"overrides/{self.ip_input.text()}/{input_id}"
        self.settings.setValue(f"{setting_key}/enabled", checkbox.isChecked())
        self.settings.setValue(f"{setting_key}/name", name_edit.text())
        if self.monitor_thread is not None:
            self.monitor_thread.set_override(input_id, name)

    def update_camera_number(self, input_id):
        if input_id not in self.input_rows:
            return
        _, _, _, camera_spin = self.input_rows[input_id]
        number = camera_spin.value()
        self.settings.setValue(f"camera_numbers/{self.ip_input.text()}/{input_id}", number)
        if self.monitor_thread is not None:
            self.monitor_thread.set_camera_number(input_id, number)

    def update_log_table(self, src, start, end, camera_number=None):
        clip = {'src': src, 'start': start, 'end': end, 'camera': camera_number}
        old_scroll_value = self.timeline_scroll.horizontalScrollBar().value()
        self.clips.append(clip)
        old_selected_row = self.log_table.currentRow()
        self.suppress_timeline_selection = True
        try:
            self.add_log_row(clip)
            if old_selected_row >= 0:
                self.log_table.selectRow(old_selected_row)
        finally:
            self.suppress_timeline_selection = False
        self.refresh_timeline()
        bar = self.timeline_scroll.horizontalScrollBar()
        bar.setValue(min(old_scroll_value, bar.maximum()))
        QTimer.singleShot(0, lambda previous=old_scroll_value: self.follow_timeline_end(previous))
        self.log_table.scrollToBottom()
        self.refresh_log_issue_summary()

    def follow_timeline_end(self, previous_value):
        bar = self.timeline_scroll.horizontalScrollBar()
        bar.setValue(min(previous_value, bar.maximum()))
        self.animate_timeline_scroll(bar.maximum(), 450)

    def camera_number_for_source(self, src):
        for _, (row, checkbox, name_edit, camera_spin) in self.input_rows.items():
            label = name_edit.text().strip() if checkbox.isChecked() else self.input_list.item(row, 1).text()
            if label == src:
                return camera_spin.value()
        atem_ip = self.ip_input.text()
        for number in range(1, 41):
            key = f"overrides/{atem_ip}/{number}"
            if (self.settings.value(f"{key}/enabled", False, type=bool)
                    and str(self.settings.value(f"{key}/name", "")).strip() == src):
                return self.settings.value(f"camera_numbers/{atem_ip}/{number}", number, type=int)
        stored = self.settings.value(f"camera_source_numbers/{src}")
        if stored is not None and str(stored).isdigit() and valid_camera_number(int(stored)):
            return int(stored)
        return suggested_camera_number(src)

    def add_log_row(self, clip):
        self.updating_log_table = True
        if clip.get('camera') is None:
            clip['camera'] = self.camera_number_for_source(clip['src'])
        row_position = self.log_table.rowCount()
        self.log_table.insertRow(row_position)
        for column, value in enumerate((clip['src'], clip['start'], clip['end'])):
            self.log_table.setItem(row_position, column, QTableWidgetItem(value))
        duration = QTableWidgetItem(display_duration(clip['start'], clip['end'], self.fps_combo.currentData()))
        duration.setFlags(duration.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.log_table.setItem(row_position, 3, duration)
        self.log_table.setItem(row_position, 4, QTableWidgetItem(str(clip['camera']) if clip['camera'] is not None else ""))
        self.updating_log_table = False
        self.validate_log_row(row_position)

    def refresh_timeline(self):
        while self.timeline_layout.count():
            item = self.timeline_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.timeline_buttons = []
        self.timeline_scroll_targets = []
        self.timeline_positions = []
        colors = ("#123ec5", "#16a32a", "#09a9a7", "#1b9d91", "#7440b5", "#bd3c2d", "#087e99")
        rate = self.fps_combo.currentData()
        fps = max(1, TIMECODE_RATES[rate][0])
        self.timeline_origin_frame = 0
        self.timeline_total_frames = 0
        if not self.clips:
            empty = QLabel("No shots yet — start logging to build the sequence")
            empty.setStyleSheet("color: #858c91; padding: 8px; font-style: italic;")
            empty.installEventFilter(self)
            self.timeline_scroll_targets.append(empty)
            self.timeline_layout.addWidget(empty)
        else:
            try:
                self.timeline_origin_frame = timecode_to_frames(self.clips[0]["start"], rate)
            except (ValueError, KeyError):
                self.timeline_origin_frame = 0
        cursor_frames = 0
        cursor_pixels = 0
        for row, clip in enumerate(self.clips):
            try:
                start_frame = timecode_to_frames(clip["start"], rate)
                end_frame = timecode_to_frames(clip["end"], rate)
                clip_offset = (start_frame - self.timeline_origin_frame) % frames_per_day(rate)
                duration_frames = (end_frame - start_frame) % frames_per_day(rate)
            except (ValueError, KeyError):
                clip_offset = cursor_frames
                duration_frames = 1
            clip_x = round(clip_offset / fps * self.timeline_pixels_per_second)
            clip_end_x = round((clip_offset + duration_frames) / fps * self.timeline_pixels_per_second)
            gap_width = max(0, clip_x - cursor_pixels)
            if gap_width:
                gap = QWidget()
                gap.setFixedSize(gap_width, 72)
                gap.setStyleSheet("background: #111315; border-bottom: 1px solid #343a3f;")
                gap.installEventFilter(self)
                self.timeline_scroll_targets.append(gap)
                self.timeline_layout.addWidget(gap)
            self.timeline_positions.append(clip_x)
            camera = clip.get("camera")
            color = colors[(int(camera) - 1) % len(colors)] if valid_camera_number(camera) else "#55585b"
            button = QPushButton(self.timeline_clip_text(clip, rate))
            block_width = max(1, clip_end_x - clip_x)
            button.setFixedSize(block_width, 72)
            button.setToolTip(f"{clip['src']}  |  {clip['start']} → {clip['end']}")
            self.style_timeline_button(button, color, row == self.log_table.currentRow())
            button.installEventFilter(self)
            if block_width < 170:
                button.setText(f"CAM {camera if valid_camera_number(camera) else '—'}")
            button.clicked.connect(lambda checked=False, index=row: self.select_timeline_clip(index))
            self.timeline_layout.addWidget(button)
            self.timeline_buttons.append(button)
            cursor_frames = max(cursor_frames, clip_offset + duration_frames)
            cursor_pixels = max(cursor_pixels, clip_end_x)
        self.timeline_total_frames = cursor_frames
        self.resize_timeline_content()

    def timeline_clip_text(self, clip, rate):
        camera = clip.get("camera")
        return (f"CAM {camera if valid_camera_number(camera) else '—'}   {clip['src']}\n"
                f"{clip['start']}  •  {display_duration(clip['start'], clip['end'], rate)}")

    def style_timeline_button(self, button, color, selected):
        edge = "#00dce8" if selected else "#17191b"
        button.setStyleSheet(
            f"text-align: left; padding: 4px; color: white; background: {color}; "
            f"border: 0; border-right: 1px solid #111315; border-top: 2px solid {edge}; "
            f"border-bottom: 2px solid {edge}; border-radius: 0; font-weight: bold;"
        )

    def resize_timeline_content(self):
        self.timeline_layout.activate()
        width = sum(item.widget().width() if item.widget() else 0
                    for index in range(self.timeline_layout.count())
                    if (item := self.timeline_layout.itemAt(index)))
        if not width and self.timeline_layout.count():
            width = self.timeline_layout.sizeHint().width()
        width = max(width, self.timeline_scroll.viewport().width())
        self.timeline_track.setFixedSize(width, 72)
        self.timeline_ruler.configure(
            self.timeline_origin_frame, self.timeline_total_frames,
            self.fps_combo.currentData(), self.timeline_pixels_per_second, width)
        self.timeline_content.setFixedSize(width, 106)

    def update_timeline_clip(self, row):
        if row < 0 or row >= len(self.clips):
            return
        if row >= len(self.timeline_buttons):
            self.refresh_timeline()
            return
        clip = self.clips[row]
        button = self.timeline_buttons[row]
        rate = self.fps_combo.currentData()
        try:
            duration_frames = (timecode_to_frames(clip["end"], rate) -
                               timecode_to_frames(clip["start"], rate)) % frames_per_day(rate)
        except (ValueError, KeyError):
            duration_frames = 1
        fps = max(1, TIMECODE_RATES[rate][0])
        try:
            start_frame = timecode_to_frames(clip["start"], rate)
            clip_offset = (start_frame - self.timeline_origin_frame) % frames_per_day(rate)
        except (ValueError, KeyError):
            clip_offset = round(self.timeline_positions[row] * fps / self.timeline_pixels_per_second)
        block_start_x = round(clip_offset / fps * self.timeline_pixels_per_second)
        block_end_x = round((clip_offset + duration_frames) / fps * self.timeline_pixels_per_second)
        block_width = max(1, block_end_x - block_start_x)
        button.setText(self.timeline_clip_text(clip, rate) if block_width >= 170 else
                       f"CAM {clip.get('camera') if valid_camera_number(clip.get('camera')) else '—'}")
        button.setFixedWidth(block_width)
        button.setToolTip(f"{clip['src']}  |  {clip['start']} → {clip['end']}")
        self.timeline_total_frames = max(self.timeline_total_frames,
                                         clip_offset + duration_frames)
        self.resize_timeline_content()

    def select_timeline_clip(self, row):
        if row >= self.log_table.rowCount():
            return
        self.log_table.selectRow(row)
        self.log_table.scrollToItem(self.log_table.item(row, 0))

    def animate_timeline_scroll(self, target, duration=300):
        bar = self.timeline_scroll.horizontalScrollBar()
        target = max(bar.minimum(), min(int(target), bar.maximum()))
        if self.timeline_scroll_animation is not None:
            self.timeline_scroll_animation.stop()
        animation = QPropertyAnimation(bar, b"value", self)
        animation.setDuration(duration)
        animation.setStartValue(bar.value())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.timeline_scroll_animation = animation
        animation.start()

    def eventFilter(self, watched, event):
        timeline_widgets = tuple(widget for widget in (
            getattr(self, "timeline_scroll", None).viewport() if getattr(self, "timeline_scroll", None) else None,
            getattr(self, "timeline_content", None), getattr(self, "timeline_track", None),
            getattr(self, "timeline_ruler", None), *getattr(self, "timeline_scroll_targets", ()))
            if widget is not None)
        if (watched in timeline_widgets or watched in getattr(self, "timeline_buttons", ())) \
                and event.type() == QEvent.Type.Wheel:
            bar = self.timeline_scroll.horizontalScrollBar()
            pixel_x = event.pixelDelta().x()
            if pixel_x:
                scroll_change = -pixel_x
            else:
                horizontal_delta = event.angleDelta().x()
                vertical_delta = event.angleDelta().y()
                scroll_change = (int(-horizontal_delta / 120 * 80) if horizontal_delta
                                 else int(vertical_delta / 120 * 80))
            if scroll_change:
                bar.setValue(bar.value() + scroll_change)
                event.accept()
                return True
        log_table = getattr(self, "log_table", None)
        if ((log_table is not None and watched in (log_table, log_table.viewport())
             or watched in getattr(self, "timeline_buttons", ()))
                and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right)):
            row = self.log_table.currentRow()
            if row >= 0:
                step = -1 if event.key() == Qt.Key.Key_Left else 1
                self.select_timeline_clip(max(0, min(row + step, self.log_table.rowCount() - 1)))
                return True
        return super().eventFilter(watched, event)

    def select_timeline_from_log(self):
        if self.suppress_timeline_selection:
            return
        row = self.log_table.currentRow()
        if row < 0:
            return
        x = self.timeline_positions[row] if row < len(self.timeline_positions) else 0
        button = self.timeline_buttons[row] if row < len(self.timeline_buttons) else None
        width = button.width() if button is not None else 1
        target = x + width // 2 - self.timeline_scroll.viewport().width() // 2
        self.animate_timeline_scroll(target)
        for index, button in enumerate(self.timeline_buttons):
            clip = self.clips[index]
            camera = clip.get("camera")
            colors = ("#123ec5", "#16a32a", "#09a9a7", "#1b9d91", "#7440b5", "#bd3c2d", "#087e99")
            color = colors[(int(camera) - 1) % len(colors)] if valid_camera_number(camera) else "#55585b"
            self.style_timeline_button(button, color, index == row)

    def update_live_end(self, end):
        if not self.clips:
            return
        row = len(self.clips) - 1
        self.updating_log_table = True
        self.clips[row]['end'] = end
        self.log_table.item(row, 2).setText(end)
        self.updating_log_table = False
        self.validate_log_row(row)
        self.refresh_log_issue_summary()
        now = time.monotonic()
        if now - self.last_timeline_live_update >= 0.2:
            self.update_timeline_clip(row)
            self.last_timeline_live_update = now

    def log_item_changed(self, item):
        if self.updating_log_table or item.row() >= len(self.clips) or item.column() == 3:
            return
        row = item.row()
        if item.column() == 4:
            value = item.text().strip()
            number = int(value) if value.isdigit() else value
            self.clips[row]['camera'] = number
            self.refresh_timeline()
            if valid_camera_number(number):
                source = self.clips[row]['src']
                self.settings.setValue(f"camera_source_numbers/{source}", number)
                self.updating_log_table = True
                for other_row, clip in enumerate(self.clips):
                    if clip['src'] == source:
                        clip['camera'] = number
                        self.log_table.item(other_row, 4).setText(str(number))
                self.updating_log_table = False
                for _, (input_row, checkbox, name_edit, camera_spin) in self.input_rows.items():
                    label = name_edit.text().strip() if checkbox.isChecked() else self.input_list.item(input_row, 1).text()
                    if label == source:
                        camera_spin.setValue(number)
            self.refresh_durations()
            return
        field = ('src', 'start', 'end')[item.column()]
        new_value = item.text().strip()
        old_value = self.clips[row][field]
        if new_value == old_value:
            return
        if field in ('start', 'end'):
            has_affected_clips = row > 0 if field == 'start' else row < len(self.clips) - 1
            try:
                timecode_to_frames(new_value, self.fps_combo.currentData())
                valid_new_timecode = True
            except ValueError:
                valid_new_timecode = False
            if has_affected_clips and valid_new_timecode:
                choice = self.ask_ripple(row, field, old_value, new_value)
                if choice == 'cancel':
                    self.updating_log_table = True
                    item.setText(old_value)
                    self.updating_log_table = False
                    return
                if choice == 'ripple':
                    try:
                        updated, removed_rows = ripple_clips(
                            self.clips, row, field, new_value, self.fps_combo.currentData())
                    except ValueError as e:
                        self.show_error("Ripple Error", f"{e}\n\nNo clip timecodes were changed.")
                        self.updating_log_table = True
                        item.setText(old_value)
                        self.updating_log_table = False
                    else:
                        if removed_rows and not self.ask_remove_ripple_clips(
                                row, field, removed_rows, new_value):
                            self.updating_log_table = True
                            item.setText(old_value)
                            self.updating_log_table = False
                            return
                        self.apply_ripple_edit(updated, removed_rows, row)
                        return
        self.clips[row][field] = new_value
        self.validate_log_row(row)
        self.refresh_log_issue_summary()
        self.refresh_timeline()

    def ask_ripple(self, row, field, old_value, new_value):
        direction = 'previous' if field == 'start' else 'next'
        message = QMessageBox(self)
        message.setWindowTitle("Ripple timecode edit?")
        message.setIcon(QMessageBox.Icon.Question)
        message.setText(f"Row {row + 1} {field.title()}: {old_value} → {new_value}")
        message.setInformativeText(
            f"Move only the {direction} clip's adjacent edit point to this time? "
            "Keep its other timecode unchanged.")
        ripple_button = message.addButton(f"Ripple {direction} clip", QMessageBox.ButtonRole.AcceptRole)
        local_button = message.addButton("Only this clip", QMessageBox.ButtonRole.ActionRole)
        message.addButton("Cancel edit", QMessageBox.ButtonRole.RejectRole)
        message.setDefaultButton(local_button)
        message.exec()
        if message.clickedButton() is ripple_button:
            return 'ripple'
        if message.clickedButton() is local_button:
            return 'local'
        return 'cancel'

    def ask_remove_ripple_clips(self, row, field, removed_rows, boundary):
        rows = ", ".join(str(index + 1) for index in sorted(removed_rows))
        direction = "following" if field == "end" else "preceding"
        message = QMessageBox(self)
        message.setWindowTitle("Remove clips made invalid?")
        message.setIcon(QMessageBox.Icon.Warning)
        message.setText(f"This ripple would remove {len(removed_rows)} {direction} clip(s).")
        message.setInformativeText(
            f"Clip row(s) {rows} would have no positive duration at {boundary}. "
            "Remove those clips and continue the edit?")
        message.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        message.setDefaultButton(QMessageBox.StandardButton.No)
        return message.exec() == QMessageBox.StandardButton.Yes

    def apply_ripple_edit(self, updated, removed_rows, edited_row):
        selected_row = self.log_table.currentRow()
        removed_before_selection = sum(index < selected_row for index in removed_rows)
        selected_was_removed = selected_row in removed_rows
        edited_row_after_removal = edited_row - sum(index < edited_row for index in removed_rows)
        if selected_row < 0 or selected_was_removed:
            selected_after_removal = edited_row_after_removal
        else:
            selected_after_removal = selected_row - removed_before_selection

        self.suppress_timeline_selection = True
        try:
            self.clips = updated
            if removed_rows:
                self.log_table.setRowCount(0)
                for clip in updated:
                    self.add_log_row(clip)
            else:
                self.updating_log_table = True
                for changed_row, clip in enumerate(updated):
                    self.log_table.item(changed_row, 1).setText(clip['start'])
                    self.log_table.item(changed_row, 2).setText(clip['end'])
                self.updating_log_table = False
                self.refresh_durations()
            if 0 <= selected_after_removal < self.log_table.rowCount():
                self.log_table.selectRow(selected_after_removal)
        finally:
            self.updating_log_table = False
            self.suppress_timeline_selection = False
        self.refresh_timeline()
        self.refresh_log_issue_summary()

    def validate_log_row(self, row):
        clip = self.clips[row]
        rate = self.fps_combo.currentData()
        problems = {}
        self.updating_log_table = True
        for column, field in ((1, 'start'), (2, 'end')):
            try:
                timecode_to_frames(clip[field], rate)
            except ValueError as e:
                problems[column] = str(e)
            item = self.log_table.item(row, column)
            item.setBackground(QBrush(QColor(255, 190, 130)) if column in problems else QBrush())
            item.setToolTip(problems.get(column, ""))
        camera = clip.get('camera')
        camera_item = self.log_table.item(row, 4)
        if valid_camera_number(camera):
            camera_item.setBackground(QBrush())
            camera_item.setToolTip("")
        else:
            camera_item.setBackground(QBrush(QColor(255, 190, 130)))
            camera_item.setToolTip("Enter the CuePilot camera number (1–48).")
        self.log_table.item(row, 3).setText(display_duration(clip['start'], clip['end'], rate))
        self.updating_log_table = False

    def frame_rate_changed(self):
        self.settings.setValue("timecode_rate", self.fps_combo.currentData())
        self.refresh_durations()
        self.refresh_timeline()
        self.refresh_timecode_warning()

    def refresh_durations(self):
        for row in range(len(self.clips)):
            self.validate_log_row(row)
        self.refresh_log_issue_summary()

    def refresh_log_issue_summary(self):
        invalid_rows = []
        unmapped_rows = []
        rate = self.fps_combo.currentData()
        for row, clip in enumerate(self.clips, 1):
            for field in ('start', 'end'):
                try:
                    timecode_to_frames(clip[field], rate)
                except ValueError:
                    invalid_rows.append(row)
            if not valid_camera_number(clip.get('camera')):
                unmapped_rows.append(row)
        messages = []
        if invalid_rows:
            rows = ', '.join(map(str, sorted(set(invalid_rows))))
            messages.append(f"{len(invalid_rows)} invalid timecode(s) in row(s) {rows}. Save Session works; correct orange cells before EDL export.")
        if unmapped_rows:
            rows = ', '.join(map(str, unmapped_rows))
            messages.append(f"Camera # needed in row(s) {rows} for CuePilot EDL export.")
        if self.detected_rate and self.detected_rate != rate.split()[0]:
            messages.append(f"HyperDeck reported {self.detected_rate} fps; export is set to {rate} fps.")
        self.log_issue_label.setText("⚠ " + " ".join(messages) if messages else "No timecode errors in log")
        self.log_issue_label.setStyleSheet("color: #c47f00; font-weight: bold;" if messages else "")

    def update_timecode(self, timecode):
        self.timecode_display.setText(timecode)
        self.refresh_timecode_warning()

    def show_rejected_timecode(self, timecode):
        self.last_rejected_timecode = timecode
        self.refresh_timecode_warning()

    def update_video_format(self, video_format):
        self.detected_video_format = video_format or None
        self.detected_rate = rate_from_video_format(video_format)
        self.detected_fps_label.setText(f"FPS: {self.detected_rate}" if self.detected_rate else "FPS: —")
        self.detected_fps_label.setToolTip(
            f"HyperDeck video format: {video_format}" if video_format else
            "Waiting for a video format reported by the HyperDeck")
        self.refresh_timecode_warning()
        self.refresh_log_issue_summary()

    def refresh_timecode_warning(self):
        timecode = self.timecode_display.text()
        selected_rate = self.fps_combo.currentData()
        if not selected_rate:
            return
        warnings = []
        try:
            timecode_to_frames(timecode, selected_rate)
        except ValueError as e:
            warnings.append(str(e))
        if ';' in timecode and not TIMECODE_RATES[selected_rate][1]:
            warnings.append("Timecode uses a drop-frame separator (;).")
        if self.detected_rate and self.detected_rate != selected_rate.split()[0]:
            warnings.append(f"HyperDeck reports {self.detected_rate} fps; export is set to {selected_rate} fps.")
        if self.last_rejected_timecode:
            warnings.append(f"Ignored invalid HyperDeck timecode {self.last_rejected_timecode}; it was not logged.")
        self.timecode_warning.setText("⚠ " + " ".join(warnings) if warnings else "")

    def is_valid_ip(self, ip):
        try:
            socket.inet_aton(ip)
            return True
        except socket.error:
            return False

    def show_error(self, title, message):
        QMessageBox.critical(self, title, message)

    def closeEvent(self, event):
        """Handle closing the application properly"""
        if not self.monitor_shutdown_complete and self.monitor_thread is not None and self.monitor_thread.isRunning():
            self.close_pending = True
            self.monitor_thread.request_stop()
            event.ignore()
            return
        self.atem_connected = False
        self.has_atem_session = False
        self.atem.disconnect()
        if self.preview_thread is not None and self.preview_thread.isRunning():
            self.preview_thread.request_stop()
            self.preview_thread.wait(3500)
        self.connect_timer.stop()
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.sync()
        event.accept()

if __name__ == "__main__":
    app = QApplication([])
    window = ATEMGUI()
    window.show()
    app.exec()
