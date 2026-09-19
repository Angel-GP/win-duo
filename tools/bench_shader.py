"""测量 Duo 折叠着色器在真实分辨率下的单帧耗时, 定位卡顿来源。

用法:
    .venv\\Scripts\\python.exe tools\\bench_shader.py
    .venv\\Scripts\\python.exe tools\\bench_shader.py --width 2560 --height 1600
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PyQt6.QtWidgets import QApplication
from OpenGL import GL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from render.shader import FS_DUO, VS  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=2560)
    ap.add_argument("--height", type=int, default=1600)
    ap.add_argument("--taps", type=int, default=32)
    ap.add_argument("--frames", type=int, default=20)
    ap.add_argument("--spread", type=float, default=0.42)
    ap.add_argument("--eye-h", type=float, default=2.0)
    args = ap.parse_args()

    W, H = args.width, args.height

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication([sys.argv[0]])

    surface = QOffscreenSurface()
    surface.create()
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    assert ctx.create(), "GL context 创建失败"
    assert ctx.makeCurrent(surface), "makeCurrent 失败"

    print("GL_VENDOR   :", GL.glGetString(GL.GL_VENDOR).decode())
    print("GL_RENDERER :", GL.glGetString(GL.GL_RENDERER).decode())
    print("GL_VERSION  :", GL.glGetString(GL.GL_VERSION).decode())
    print("分辨率      : %dx%d  = %.2f M 像素" % (W, H, W * H / 1e6))
    print("max_taps    : %d" % args.taps)
    print("-" * 68)

    prog = QOpenGLShaderProgram()
    assert prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VS)
    assert prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FS_DUO), prog.log()
    assert prog.link(), prog.log()
    prog.bind()

    # 纹理: 真假数据都行, 这里用噪声保证 mip 生成不会被优化掉
    rng = np.random.default_rng(1)
    data = rng.integers(0, 256, (H, W, 4), dtype=np.uint8)
    tex = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, W, H, 0,
                    GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                       GL.GL_LINEAR_MIPMAP_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
    GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
    bd = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, bd)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, W, H, 0,
                    GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                       GL.GL_LINEAR_MIPMAP_LINEAR)
    GL.glGenerateMipmap(GL.GL_TEXTURE_2D)

    fbo = GL.glGenFramebuffers(1)
    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
    out_tex = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, out_tex)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, W, H, 0, GL.GL_RGBA,
                    GL.GL_UNSIGNED_BYTE, None)
    GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                              GL.GL_TEXTURE_2D, out_tex, 0)
    assert GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER) == GL.GL_FRAMEBUFFER_COMPLETE

    GL.glViewport(0, 0, W, H)
    u = prog.uniformLocation
    GL.glActiveTexture(GL.GL_TEXTURE0)
    GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
    GL.glUniform1i(u("uTex"), 0)
    GL.glActiveTexture(GL.GL_TEXTURE1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, bd)
    GL.glUniform1i(u("uBackdrop"), 1)
    GL.glUniform2f(u("uRes"), float(W), float(H))
    GL.glUniform1f(u("uEyeZ"), args.eye_h * H)
    GL.glUniform1f(u("uSpread"), args.spread)
    GL.glUniform1f(u("uDark"), 0.001)
    GL.glUniform1i(u("uMaxTaps"), args.taps)

    print("%8s %10s %12s %10s" % ("倾角", "单帧 ms", "理论最高FPS", "16.7ms 预算"))
    for tilt in (0.0, 10.0, 30.0, 45.0, 60.0, 75.0, 88.0):
        GL.glUniform1f(u("uTilt"), tilt * 3.14159265 / 180.0)
        GL.glUniform1i(u("uOutside"), 0)
        # 预热
        for _ in range(3):
            GL.glBegin(GL.GL_QUADS)
            GL.glTexCoord2f(0, 0); GL.glVertex2f(-1, -1)
            GL.glTexCoord2f(1, 0); GL.glVertex2f(1, -1)
            GL.glTexCoord2f(1, 1); GL.glVertex2f(1, 1)
            GL.glTexCoord2f(0, 1); GL.glVertex2f(-1, 1)
            GL.glEnd()
            GL.glFinish()
        t0 = time.perf_counter()
        for _ in range(args.frames):
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            GL.glBegin(GL.GL_QUADS)
            GL.glTexCoord2f(0, 0); GL.glVertex2f(-1, -1)
            GL.glTexCoord2f(1, 0); GL.glVertex2f(1, -1)
            GL.glTexCoord2f(1, 1); GL.glVertex2f(1, 1)
            GL.glTexCoord2f(0, 1); GL.glVertex2f(-1, 1)
            GL.glEnd()
            GL.glFinish()
        ms = (time.perf_counter() - t0) / args.frames * 1000.0
        print("%8.0f %10.2f %12.0f %10s"
              % (tilt, ms, 1000.0 / ms if ms > 0 else 0,
                 "OK" if ms <= 16.7 else "超预算 %.1fx" % (ms / 16.7)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
