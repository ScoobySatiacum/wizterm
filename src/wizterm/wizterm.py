import os
from pathlib import Path
import subprocess
import sys
import pyte
import psutil
from PyQt6.QtCore import QObject, QSocketNotifier, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QMainWindow, QPlainTextEdit

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
  from winpty import PtyProcess
else:
  import pty


class PtyBackend(QObject):
  data_received = pyqtSignal(str)
  closed = pyqtSignal()

  def write(self, data: str):
    raise NotImplementedError

  def resize(self, cols: int, rows: int):
    raise NotImplementedError

  def close(self):
    raise NotImplementedError

  def get_cwd(self) -> str:
    raise NotImplementedError


class UnixPtyBackend(PtyBackend):

  def __init__(self, cols=80, rows=24, parent=None):
    super().__init__(parent)
    self.master_fd, slave_fd = pty.openpty()
    shell = os.environ.get("SHELL", "/bin/bash")

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    self.process = subprocess.Popen(
        [shell],
        preexec_fn=os.setsid,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        universal_newlines=False,
    )
    os.close(slave_fd)
    self.resize(cols, rows)

    self.notifier = QSocketNotifier(
        self.master_fd, QSocketNotifier.Type.Read, self
    )
    self.notifier.activated.connect(self._on_read)

  def _on_read(self):
    try:
      data = os.read(self.master_fd, 4096)
      if data:
        self.data_received.emit(data.decode("utf-8", errors="replace"))
    except OSError:
      self.notifier.setEnabled(False)
      self.closed.emit()

  def write(self, data: str):
    os.write(self.master_fd, data.encode("utf-8"))

  def resize(self, cols: int, rows: int):
    import fcntl
    import struct
    import termios

    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

  def close(self):
    self.notifier.setEnabled(False)
    try:
      os.close(self.master_fd)
    except OSError:
      pass
    self.process.terminate()

  def get_cwd(self) -> str:
    try:
      proc = psutil.Process(self.process.pid)
      # If a child command (e.g., vim, python) is running, check it first
      children = proc.children(recursive=True)
      target = children[-1] if children else proc
      return target.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
      return ""


class WindowsReadThread(QThread):
  data_received = pyqtSignal(str)
  closed = pyqtSignal()

  def __init__(self, proc):
    super().__init__()
    self.proc = proc

  def run(self):
    try:
      while self.proc.isalive():
        data = self.proc.read(4096)
        if data:
          self.data_received.emit(data)
    except EOFError:
      pass
    finally:
      self.closed.emit()


class WindowsPtyBackend(PtyBackend):

  def __init__(self, cols=80, rows=24, parent=None):
    super().__init__(parent)
    shell = os.environ.get("COMSPEC", "cmd.exe")
    self.proc = PtyProcess.spawn(shell, dimensions=(rows, cols))

    self.reader_thread = WindowsReadThread(self.proc)
    self.reader_thread.data_received.connect(self.data_received.emit)
    self.reader_thread.closed.connect(self.closed.emit)
    self.reader_thread.start()

  def write(self, data: str):
    if self.proc.isalive():
      self.proc.write(data)

  def resize(self, cols: int, rows: int):
    if self.proc.isalive():
      self.proc.setwinsize(rows, cols)

  def close(self):
    if self.proc.isalive():
      self.proc.terminate(force=True)
    self.reader_thread.wait(500)

  def get_cwd(self) -> str:
    try:
      # PtyProcess exposes the underlying shell's PID via .pid
      proc = psutil.Process(self.proc.pid)
      children = proc.children(recursive=True)
      target = children[-1] if children else proc
      return target.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
      return ""

class TerminalWidget(QPlainTextEdit):

  def __init__(self, cols=100, rows=30, parent=None):
    super().__init__(parent)
    self.cols = cols
    self.rows = rows

    self.setFont(QFont("Consolas" if IS_WINDOWS else "Menlo", 11))
    self.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4;")
    self.setReadOnly(True)

    # Set up pyte terminal emulator screen and stream
    self.screen = pyte.Screen(self.cols, self.rows)
    self.stream = pyte.Stream(self.screen)

    self.backend = (
        WindowsPtyBackend(self.cols, self.rows, self)
        if IS_WINDOWS
        else UnixPtyBackend(self.cols, self.rows, self)
    )
    self.backend.data_received.connect(self.on_data_received)

  def on_data_received(self, text: str):
    # Feed raw PTY output into pyte instead of inserting directly
    self.stream.feed(text)
    self.render_screen()

  def render_screen(self):
    # Display the clean terminal buffer lines
    lines = [line.rstrip() for line in self.screen.display]
    # Trim trailing empty lines so scrollbar stays clean
    while lines and not lines[-1]:
      lines.pop()
    self.setPlainText("\n".join(lines))

    scrollbar = self.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())

  def keyPressEvent(self, event):
    key = event.key()
    modifiers = event.modifiers()

    if (
        modifiers == Qt.KeyboardModifier.ControlModifier
        and key == Qt.Key.Key_C
    ):
      self.backend.write("\x03")
    elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
      row_text = self.screen.display[self.screen.cursor.y]

      # 2. Slice up to the cursor's X position (in case there is trailing text)
      line_up_to_cursor = row_text[: self.screen.cursor.x]

      # 3. Strip the shell prompt (e.g., "C:\Users\Name>" or "$ ")
      if ">" in line_up_to_cursor:
        command = line_up_to_cursor.split(">", 1)[1].strip()
      elif "$" in line_up_to_cursor:
        command = line_up_to_cursor.split("$", 1)[1].strip()
      else:
        command = line_up_to_cursor.strip()

      if command in ['ls'] and IS_WINDOWS:
        cwd = Path(self.backend.get_cwd())
        dirs = []
        files = []
        for path in cwd.glob('*'):
          if path.is_dir():
            dirs.append(str(path))
          else:
            files.append(str(path))
        self.backend.write(f'Dirs: \n{'\n'.join(dirs)} \n Files: \n{'\n'.join(files)}')

      self.backend.write("\r\n" if IS_WINDOWS else "\r")
    elif key == Qt.Key.Key_Backspace:
      self.backend.write("\x08" if IS_WINDOWS else "\x7f")
    elif key == Qt.Key.Key_Up:
      self.backend.write("\x1b[A")
    elif key == Qt.Key.Key_Down:
      self.backend.write("\x1b[B")
    elif key == Qt.Key.Key_Right:
      self.backend.write("\x1b[C")
    elif key == Qt.Key.Key_Left:
      self.backend.write("\x1b[D")
    elif event.text():
      self.backend.write(event.text())

  def closeEvent(self, event):
    self.backend.close()
    super().closeEvent(event)


if __name__ == "__main__":
  app = QApplication(sys.argv)
  window = QMainWindow()
  window.setWindowTitle("Cross-Platform PyQt6 Terminal")
  window.resize(850, 520)
  window.setCentralWidget(TerminalWidget())
  window.show()
  sys.exit(app.exec())
