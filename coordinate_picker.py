
import tkinter as tk
import pyautogui
from PIL import Image, ImageTk

# 창을 띄우기 전에 실제 화면 캡처
screenshot = pyautogui.screenshot()
original_width, original_height = screenshot.size

# 선택기 창에 맞게 이미지 축소
max_width = 1400
max_height = 780
scale = min(
    max_width / original_width,
    max_height / original_height,
    1.0
)

display_width = int(original_width * scale)
display_height = int(original_height * scale)

display_image = screenshot.resize(
    (display_width, display_height)
)

root = tk.Tk()
root.title("Desktop MCP - Coordinate Picker")
root.resizable(False, False)

photo = ImageTk.PhotoImage(display_image)

canvas = tk.Canvas(
    root,
    width=display_width,
    height=display_height,
    cursor="crosshair",
    highlightthickness=0
)
canvas.pack()
canvas.create_image(0, 0, image=photo, anchor="nw")

status = tk.Label(
    root,
    text="원하는 지점을 클릭하세요. Enter=복사 후 종료 / Esc=취소",
    padx=10,
    pady=8
)
status.pack()

selected = {"x": None, "y": None}
marker = None


def select_point(event):
    global marker

    # 표시 이미지 좌표를 원본 화면 좌표로 변환
    x = round(event.x * original_width / display_width)
    y = round(event.y * original_height / display_height)

    selected["x"] = x
    selected["y"] = y

    if marker is not None:
        canvas.delete(marker)

    marker = canvas.create_oval(
        event.x - 6, event.y - 6,
        event.x + 6, event.y + 6,
        outline="red",
        width=2
    )

    status.config(
        text=f"선택 좌표: x={x}, y={y}  |  Enter=복사 후 종료 / Esc=취소"
    )


def confirm(event=None):
    if selected["x"] is None:
        status.config(text="먼저 이미지 위의 지점을 클릭하세요.")
        return

    result = f'x={selected["x"]}, y={selected["y"]}'

    root.clipboard_clear()
    root.clipboard_append(result)
    root.update()

    print(f"\n선택한 실제 화면 좌표: {result}")
    root.destroy()


def cancel(event=None):
    print("\n좌표 선택을 취소했습니다.")
    root.destroy()


canvas.bind("<Button-1>", select_point)
root.bind("<Return>", confirm)
root.bind("<Escape>", cancel)

root.mainloop()
