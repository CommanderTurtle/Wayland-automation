import subprocess
import re
import json
import shutil

def get_geometry():
    """Return logical desktop (x, y, width, height), including negative origins.

    Hyprland supplies scale and transform information directly. Other wlroots
    compositors use the existing wayland-info fallback at origin (0, 0).
    """
    if shutil.which("hyprctl"):
        result = subprocess.run(["hyprctl", "-j", "monitors"],
                                capture_output=True, text=True, check=True,
                                timeout=5)
        monitors = json.loads(result.stdout)
        rectangles = []
        for monitor in monitors:
            if monitor.get("disabled"):
                continue
            width, height = monitor["width"], monitor["height"]
            if monitor.get("transform", 0) % 2:
                width, height = height, width
            scale = monitor.get("scale", 1)
            if scale <= 0:
                raise ValueError("Invalid monitor scale")
            rectangles.append((monitor["x"], monitor["y"],
                               round(width / scale), round(height / scale)))
        if rectangles:
            x = min(r[0] for r in rectangles)
            y = min(r[1] for r in rectangles)
            return (x, y, max(r[0] + r[2] for r in rectangles) - x,
                    max(r[1] + r[3] for r in rectangles) - y)
    width, height = get_resolution(use_hyprland=False)
    return 0, 0, width, height

def get_resolution(use_hyprland=True):
    """Return integer (width, height), or raise rather than returning an error string."""
    if use_hyprland and shutil.which("hyprctl"):
        return get_geometry()[2:]
    output = subprocess.run(["wayland-info"], capture_output=True, text=True,
                            check=True, timeout=5)
    match = re.search(r"width:\s*(\d+)\s*px,\s*height:\s*(\d+)\s*px", output.stdout)
    if not match:
        raise RuntimeError("No output resolution reported by wayland-info")
    return int(match.group(1)), int(match.group(2))

if __name__ == "__main__":
    print(get_resolution())
