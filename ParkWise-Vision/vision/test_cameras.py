"""Quick camera identifier. Shows each working camera one at a time.
Press any key to move to the next camera, Q to quit."""
import cv2

for idx in range(4):
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap.release()
        continue
    print(f"Showing camera {idx} — press any key for next, Q to quit.")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        cv2.putText(frame, f"CAMERA INDEX {idx}", (20, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)
        cv2.imshow("Camera Test", frame)
        key = cv2.waitKey(1) & 0xFF
        if key != 255:
            cap.release()
            if key == ord('q'):
                cv2.destroyAllWindows()
                raise SystemExit
            break
cv2.destroyAllWindows()
