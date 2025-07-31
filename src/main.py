import sys
import mss
from PIL import Image
import io
import base64
import threading
import queue

from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QComboBox, QHBoxLayout, QLineEdit, QPushButton, QSizePolicy
from PyQt5.QtGui import QPixmap, QImage, QPainter, QIntValidator, QCursor
from PyQt5.QtCore import QTimer, Qt, QRect, QPoint

from flask import Flask, render_template
from flask_socketio import SocketIO

class ImageWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmap = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def setPixmap(self, pixmap):
        self.pixmap = pixmap
        self.update()

    def paintEvent(self, event):
        if not self.pixmap:
            return
        painter = QPainter(self)
        scaled_pixmap = self.pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        target_rect = scaled_pixmap.rect()
        target_rect.moveCenter(self.rect().center())
        painter.drawPixmap(target_rect.topLeft(), scaled_pixmap)

class ServerThread(threading.Thread):
    def __init__(self, image_queue, host, port):
        super().__init__()
        self.image_queue = image_queue
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        self.socketio = SocketIO(self.app, async_mode='eventlet')
        
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)

        @self.app.route('/')
        def index():
            return render_template('index.html')

        @self.socketio.on('connect')
        def handle_connect():
            self.socketio.start_background_task(self.frame_sender)

    def frame_sender(self):
        while True:
            payload = self.image_queue.get()

            self.socketio.emit('screen_update', payload)
            self.socketio.sleep(0)

    def run(self):
        print(f"Starting Flask server on http://{self.host}:{self.port}")
        self.socketio.run(self.app, host=self.host, port=self.port, log_output=False)
        
    def stop(self):
        self.socketio.stop()

class ScreenX(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ScreenX Server")
        self.setGeometry(100, 100, 800, 700)
        
        self.main_widget = QWidget()
        self.setCentralWidget(self.main_widget)
        self.main_layout = QVBoxLayout()
        self.main_widget.setLayout(self.main_layout)


        controls_widget = QWidget()
        controls_layout = QVBoxLayout()
        controls_widget.setLayout(controls_layout)
        controls_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

 
        monitor_layout = QHBoxLayout()
        monitor_layout.addWidget(QLabel("Monitor:"))
        self.monitor_dropdown = QComboBox()
        monitor_layout.addWidget(self.monitor_dropdown)
        controls_layout.addLayout(monitor_layout)
        
        port_layout = QHBoxLayout()
        port_layout.addWidget(QLabel("Port:"))
        self.port_input = QLineEdit("3900")
        self.port_input.setValidator(QIntValidator(1024, 65535, self))
        port_layout.addWidget(self.port_input)
        controls_layout.addLayout(port_layout)
        
        fps_layout = QHBoxLayout()
        fps_layout.addWidget(QLabel("Capture FPS:"))
        self.fps_dropdown = QComboBox()
        self.fps_dropdown.addItems(["15", "24", "30", "60"])
        self.fps_dropdown.setCurrentText("30")
        fps_layout.addWidget(self.fps_dropdown)
        controls_layout.addLayout(fps_layout)

        self.start_button = QPushButton("Start Server")
        self.start_button.clicked.connect(self.toggle_server)
        controls_layout.addWidget(self.start_button)
        
        self.status_label = QLabel("Server is stopped.")
        controls_layout.addWidget(self.status_label)
        
        self.main_layout.addWidget(controls_widget)

        self.local_viewer = ImageWidget()
        self.local_viewer.setStyleSheet("background-color: black;")
        self.main_layout.addWidget(self.local_viewer)

        self.sct = mss.mss()
        self.populate_monitors()
        self.image_queue = queue.Queue(maxsize=2)
        self.server_thread = None
        self.is_server_running = False
        self.last_frame = None
        self.last_cursor_pos = None


        self.timer = QTimer()
        self.timer.timeout.connect(self.update_screenshot)

    def populate_monitors(self):
        for i, mon in enumerate(self.sct.monitors[1:], 1):
            self.monitor_dropdown.addItem(f"Display {i}: {mon['width']}x{mon['height']} @ {mon['left']},{mon['top']}", userData=i)

    def toggle_server(self):
        if not self.is_server_running:
            self.last_frame = None
            self.last_cursor_pos = None
            try:
                port = int(self.port_input.text())
                self.server_thread = ServerThread(self.image_queue, "0.0.0.0", port)
                self.server_thread.daemon = True
                self.server_thread.start()
                
                interval = 1000 // int(self.fps_dropdown.currentText())
                self.timer.start(interval)

                self.is_server_running = True
                self.start_button.setText("Stop Server")
                self.status_label.setText(f"Server running at http://0.0.0.0:{port}")
                self.port_input.setEnabled(False)
                self.monitor_dropdown.setEnabled(False)
            except Exception as e:
                self.status_label.setText(f"Error starting server: {e}")
        else:
            self.timer.stop()
            self.is_server_running = False
            self.start_button.setText("Start Server")
            self.status_label.setText("Server is stopped.")
            self.port_input.setEnabled(True)
            self.monitor_dropdown.setEnabled(True)

    def update_screenshot(self):
        monitor_index = self.monitor_dropdown.currentData()
        mon = self.sct.monitors[monitor_index]
        
        cursor_pos_global = QCursor.pos()
        sct_img = self.sct.grab(mon)
        
        monitor_rect = QRect(mon['left'], mon['top'], mon['width'], mon['height'])
        relative_pos = cursor_pos_global - QPoint(mon['left'], mon['top']) if monitor_rect.contains(cursor_pos_global) else None
        
        pil_img = Image.frombytes("RGB", sct_img.size, sct_img.rgb, "raw", "RGB")
        with io.BytesIO() as buffer:
            pil_img.save(buffer, 'JPEG', quality=75)
            jpeg_bytes = buffer.getvalue()
            
            screen_changed = (jpeg_bytes != self.last_frame)
            cursor_changed = (relative_pos != self.last_cursor_pos)

            payload = {}
            
            if screen_changed:
                q_image = QImage(bytes(sct_img.bgra), sct_img.width, sct_img.height, QImage.Format_ARGB32)
                self.local_viewer.setPixmap(QPixmap.fromImage(q_image))
                
                self.last_frame = jpeg_bytes
                payload['image_data'] = base64.b64encode(jpeg_bytes).decode('utf-8')

            if cursor_changed:
                self.last_cursor_pos = relative_pos
                payload['cursor_pos'] = {'x': relative_pos.x(), 'y': relative_pos.y()} if relative_pos else None
                    
            if payload:
                if not self.image_queue.full():
                    self.image_queue.put(payload)

    def closeEvent(self, event):
        print("Closing application.")
        self.timer.stop()
        self.sct.close()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ScreenX()
    window.show()
    sys.exit(app.exec_())