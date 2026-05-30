import cv2
import pytesseract
import numpy as np

# If Tesseract is not on PATH, set the executable path explicitly:
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

def preprocess(frame):
    # Crop to center 60% of frame where plate is likely to be
    h, w = frame.shape[:2]
    cropped = frame[int(h*0.2):int(h*0.8), int(w*0.1):int(w*0.9)]
    # Scale up 2x for better OCR accuracy
    scaled = cv2.resize(cropped, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh

def read_plate(frame):
    processed = preprocess(frame)
    # psm 6 = uniform block of text, no whitelist so dashes are captured too
    config = "--psm 6"
    text = pytesseract.image_to_string(processed, config=config).strip()
    return text, processed

cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)
if not cap.isOpened():
    print("Error: Could not open webcam.")
    exit(1)

print("Hold a paper plate in front of the camera.")
print("Press S to scan, Q to quit.")

while True:
    ret, frame = cap.read()
    if not ret:
        break

    h, w = frame.shape[:2]
    cv2.rectangle(frame, (int(w*0.1), int(h*0.2)), (int(w*0.9), int(h*0.8)), (0, 255, 0), 2)
    cv2.putText(frame, "S=Scan  Q=Quit", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    cv2.imshow("ParkWise - Plate Test", frame)

    key = cv2.waitKey(1) & 0xFF

    if key == ord('s'):
        text, processed = read_plate(frame)
        print(f"Detected plate: '{text}'" if text else "Nothing detected - try better lighting.")
        cv2.imshow("Processed", processed)

    elif key == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
