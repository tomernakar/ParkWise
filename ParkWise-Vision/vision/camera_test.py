import cv2

cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)
if not cap.isOpened():
    print("Error: Could not open webcam.")
    exit(1)

print("Webcam opened. Press Q to quit.")

while True:
    ret, frame = cap.read()
    if not ret:
        print("Error: Failed to read frame.")
        break

    cv2.imshow("ParkWise - Camera Test", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
