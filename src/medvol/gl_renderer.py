"""Hidden-context OpenGL 3.3 FBO renderer and deterministic readback."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from .camera import Camera
from .config import SamplingConfig
from .errors import BusinessCode, VolumeRenderError
from .raycast import linear_to_srgb
from .shaders import FRAGMENT_SHADER, VERTEX_SHADER
from .transfer_function import TransferFunction
from .volume import Volume


def checked_readback_size(
    buffer: np.ndarray,
    width: int,
    height: int,
    *,
    components: int = 4,
    itemsize: int = 4,
) -> None:
    expected = int(width) * int(height) * int(components) * int(itemsize)
    actual = int(np.asarray(buffer).nbytes)
    if actual != expected:
        raise VolumeRenderError(
            BusinessCode.E_BUFFER_SIZE,
            "OpenGL readback buffer size does not match the requested resolution",
            details={
                "width": int(width),
                "height": int(height),
                "components": int(components),
                "itemsize": int(itemsize),
                "expected_bytes": expected,
                "actual_bytes": actual,
            },
        )


def _gl_error(call_site: str, detail: dict[str, Any] | None = None, message: str | None = None) -> VolumeRenderError:
    return VolumeRenderError(
        BusinessCode.E_GL_CONTEXT,
        message or f"OpenGL operation failed: {call_site}",
        details={"call_site": call_site, **(detail or {})},
    )


@dataclass
class GLRenderResult:
    rgba_linear: np.ndarray
    rgba_srgb: np.ndarray
    report: dict[str, Any]
    business_code: BusinessCode = BusinessCode.OK

    @property
    def rgba(self) -> np.ndarray:
        return self.rgba_srgb if self.report["outputs"]["srgb_selected"] else self.rgba_linear

    @property
    def rgba_linear_sha256(self) -> str:
        return self.report["outputs"]["rgba_linear_sha256"]

    def to_envelope(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "ok": self.business_code == BusinessCode.OK,
            "status": self.business_code.value,
            "business_code": self.business_code.value,
            "report": self.report,
        }

    def report_json(self) -> str:
        return json.dumps(self.to_envelope(), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_float32(buffer: np.ndarray) -> str:
    array = np.ascontiguousarray(np.asarray(buffer, dtype=np.float32).astype("<f4", copy=False))
    return hashlib.sha256(array.tobytes(order="C")).hexdigest()


class OffscreenGLRenderer:
    """OpenGL 3.3 core-profile ray caster backed by a hidden window and FBO.

    The window is never made visible. The generated RGBA32F texture remains
    attached to the FBO for same-context UI integration and the readback array
    is a deterministic top-to-bottom copy.
    """

    def __init__(
        self,
        volume: Volume,
        camera: Camera,
        transfer_function: TransferFunction,
        config: SamplingConfig,
        *,
        width: int = 512,
        height: int = 512,
    ) -> None:
        if width <= 0 or height <= 0:
            raise VolumeRenderError(
                BusinessCode.E_BUFFER_SIZE,
                "FBO resolution must be positive",
                details={"width": width, "height": height},
            )
        minimum_step = 0.1 * volume.min_spacing
        if config.base_step < minimum_step - 1.0e-15:
            raise VolumeRenderError(
                BusinessCode.E_STEP_TOO_SMALL,
                "base_step is smaller than 0.1 times the minimum voxel spacing",
                details={
                    "base_step": config.base_step,
                    "minimum_voxel_spacing": volume.min_spacing,
                    "minimum_allowed_step": minimum_step,
                },
            )
        if config.adaptive and config.finest_step < minimum_step - 1.0e-15:
            raise VolumeRenderError(
                BusinessCode.E_STEP_TOO_SMALL,
                "adaptive subdivision would create a step smaller than 0.1 times the minimum voxel spacing",
                details={
                    "finest_step": config.finest_step,
                    "minimum_voxel_spacing": volume.min_spacing,
                    "minimum_allowed_step": minimum_step,
                },
            )
        self.volume = volume
        self.camera = camera
        self.transfer_function = transfer_function
        self.config = config
        self.lut = transfer_function.build_lut()
        self.width = int(width)
        self.height = int(height)
        self._closed = False
        self._glfw = None
        self._window = None
        self._program = 0
        self._vao = 0
        self._volume_texture = 0
        self._lut_texture = 0
        self._fbo = 0
        self._color_texture = 0
        self._fbo_width = 0
        self._fbo_height = 0
        self._uniforms: dict[str, int] = {}
        self._gl = None
        self._gl_version = ""
        self._gl_renderer = ""
        self._live_objects: set[tuple[str, int]] = set()
        try:
            self._initialize()
            self._ensure_fbo(self.width, self.height)
        except Exception as exc:
            self.close()
            if isinstance(exc, VolumeRenderError):
                raise
            raise _gl_error("initialization", {"exception_type": type(exc).__name__}, "Unable to initialize the OpenGL 3.3 context") from exc

    def _initialize(self) -> None:
        try:
            import glfw
            from OpenGL import GL
        except Exception as exc:
            raise _gl_error("import", {"exception_type": type(exc).__name__}, "PyOpenGL or GLFW is unavailable") from exc
        self._glfw = glfw
        self._gl = GL
        if not glfw.init():
            raise _gl_error("glfw.init", {}, "GLFW initialization failed")
        glfw.default_window_hints()
        glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
        glfw.window_hint(glfw.DOUBLEBUFFER, glfw.FALSE)
        self._window = glfw.create_window(1, 1, "medvol-offscreen", None, None)
        if self._window is None:
            raise _gl_error("glfw.create_window", {}, "Failed to create the hidden OpenGL context")
        glfw.make_context_current(self._window)
        glfw.swap_interval(0)
        version_bytes = GL.glGetString(GL.GL_VERSION)
        renderer_bytes = GL.glGetString(GL.GL_RENDERER)
        self._gl_version = version_bytes.decode("utf-8", "replace") if version_bytes else ""
        self._gl_renderer = renderer_bytes.decode("utf-8", "replace") if renderer_bytes else ""
        match = re.match(r"\s*(\d+)\.(\d+)", self._gl_version)
        if not match or (int(match.group(1)), int(match.group(2))) < (3, 3):
            raise _gl_error(
                "version_check",
                {"gl_version": self._gl_version, "gl_renderer": self._gl_renderer},
                "An OpenGL 3.3 or newer core context is required",
            )
        self._program = self._build_program(VERTEX_SHADER, FRAGMENT_SHADER)
        self._vao = self._track("vao", GL.glGenVertexArrays(1))
        self._volume_texture = self._track("volume_texture", GL.glGenTextures(1))
        self._lut_texture = self._track("lut_texture", GL.glGenTextures(1))
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        self._upload_volume()
        self._upload_lut()
        self._uniforms = {
            name: GL.glGetUniformLocation(self._program, name)
            for name in (
                "uVolume",
                "uTransferLut",
                "uCameraPosition",
                "uCameraForward",
                "uCameraRight",
                "uCameraTrueUp",
                "uResolution",
                "uTanHalfFov",
                "uBoundsMin",
                "uBoundsMax",
                "uOrigin",
                "uSpacing",
                "uVolumeShape",
                "uValueMin",
                "uValueMax",
                "uLutStep",
                "uLutSize",
                "uTransferMaxGradient",
                "uBaseStep",
                "uAdaptive",
                "uMaxRefinement",
                "uGradientThreshold",
                "uEarlyThreshold",
                "uMaxBlocks",
                "uMaxSubdivisions",
            )
        }

    def _track(self, kind: str, identifier: int) -> int:
        self._live_objects.add((kind, int(identifier)))
        return int(identifier)

    def _untrack(self, kind: str, identifier: int) -> None:
        self._live_objects.discard((kind, int(identifier)))

    def _build_program(self, vertex_source: str, fragment_source: str) -> int:
        GL = self._gl
        shader_ids: list[int] = []

        def compile_shader(stage: int, source: str, label: str) -> int:
            shader = GL.glCreateShader(stage)
            if not shader:
                raise _gl_error(f"glCreateShader({label})")
            shader_ids.append(int(shader))
            GL.glShaderSource(shader, source)
            GL.glCompileShader(shader)
            if not GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS):
                log = GL.glGetShaderInfoLog(shader)
                text = log.decode("utf-8", "replace") if isinstance(log, bytes) else str(log)
                raise _gl_error("shader_compile", {"stage": label, "log": text})
            return int(shader)

        try:
            vertex = compile_shader(GL.GL_VERTEX_SHADER, vertex_source, "vertex")
            fragment = compile_shader(GL.GL_FRAGMENT_SHADER, fragment_source, "fragment")
            program = GL.glCreateProgram()
            if not program:
                raise _gl_error("glCreateProgram")
            program = self._track("program", program)
            GL.glAttachShader(program, vertex)
            GL.glAttachShader(program, fragment)
            GL.glLinkProgram(program)
            if not GL.glGetProgramiv(program, GL.GL_LINK_STATUS):
                log = GL.glGetProgramInfoLog(program)
                text = log.decode("utf-8", "replace") if isinstance(log, bytes) else str(log)
                raise _gl_error("shader_link", {"log": text})
            return program
        finally:
            for shader in shader_ids:
                GL.glDeleteShader(shader)
            shader_ids.clear()

    def _upload_volume(self) -> None:
        GL = self._gl
        GL.glBindTexture(GL.GL_TEXTURE_3D, self._volume_texture)
        GL.glTexParameteri(GL.GL_TEXTURE_3D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_3D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_3D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_3D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_3D, GL.GL_TEXTURE_WRAP_R, GL.GL_CLAMP_TO_EDGE)
        width, height, depth = self.volume.shape_xyz
        data = self.volume.data
        if data.dtype != np.uint16:
            if data.dtype.kind in "iu" and int(np.min(data)) >= 0 and int(np.max(data)) <= 65535:
                data = data.astype(np.uint16)
            elif data.dtype.kind == "f":
                data = np.clip(np.floor(data + 0.5), 0, 65535).astype(np.uint16)
            else:
                raise _gl_error("volume_upload", {"dtype": str(data.dtype)}, "GPU volume path requires values representable as uint16")
        GL.glTexImage3D(
            GL.GL_TEXTURE_3D,
            0,
            GL.GL_R16UI,
            width,
            height,
            depth,
            0,
            GL.GL_RED_INTEGER,
            GL.GL_UNSIGNED_SHORT,
            np.ascontiguousarray(data),
        )

    def _upload_lut(self) -> None:
        GL = self._gl
        GL.glBindTexture(GL.GL_TEXTURE_1D, self._lut_texture)
        GL.glTexParameteri(GL.GL_TEXTURE_1D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_1D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_1D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexImage1D(
            GL.GL_TEXTURE_1D,
            0,
            GL.GL_RGBA32F,
            int(self.lut.rgba.shape[0]),
            0,
            GL.GL_RGBA,
            GL.GL_FLOAT,
            np.ascontiguousarray(self.lut.rgba),
        )

    def _ensure_fbo(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise VolumeRenderError(
                BusinessCode.E_BUFFER_SIZE,
                "FBO resolution must be positive",
                details={"width": width, "height": height},
            )
        if self._fbo_width == width and self._fbo_height == height:
            return
        GL = self._gl
        if self._fbo:
            GL.glDeleteFramebuffers(1, [self._fbo])
            self._untrack("fbo", self._fbo)
        if self._color_texture:
            GL.glDeleteTextures(1, [self._color_texture])
            self._untrack("color_texture", self._color_texture)
        self._fbo = 0
        self._color_texture = 0
        texture = self._track("color_texture", GL.glGenTextures(1))
        GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA32F, width, height, 0, GL.GL_RGBA, GL.GL_FLOAT, None)
        fbo = self._track("fbo", GL.glGenFramebuffers(1))
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, texture, 0)
        status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
        if status != GL.GL_FRAMEBUFFER_COMPLETE:
            raise _gl_error("framebuffer_status", {"status": hex(int(status))})
        self._color_texture = texture
        self._fbo = fbo
        self._fbo_width = int(width)
        self._fbo_height = int(height)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)

    def make_current(self) -> None:
        self._assert_open()
        self._glfw.make_context_current(self._window)

    def _assert_open(self) -> None:
        if self._closed or self._window is None:
            raise _gl_error("context_state", {}, "The OpenGL context has already been destroyed")

    @property
    def color_texture_id(self) -> int:
        return int(self._color_texture)

    @property
    def live_object_count(self) -> int:
        return len(self._live_objects)

    def _set_uniforms(self, width: int, height: int) -> None:
        GL = self._gl
        uniforms = self._uniforms

        def set1(name: str, value: float) -> None:
            GL.glUniform1f(uniforms[name], float(value))

        def set1i(name: str, value: int) -> None:
            GL.glUniform1i(uniforms[name], int(value))

        def set2(name: str, value: tuple[float, float]) -> None:
            GL.glUniform2f(uniforms[name], float(value[0]), float(value[1]))

        def set3(name: str, value: np.ndarray) -> None:
            GL.glUniform3f(uniforms[name], float(value[0]), float(value[1]), float(value[2]))

        GL.glUseProgram(self._program)
        set1i("uVolume", 0)
        set1i("uTransferLut", 1)
        set3("uCameraPosition", np.asarray(self.camera.position, dtype=np.float64))
        set3("uCameraForward", self.camera.forward)
        set3("uCameraRight", self.camera.right)
        set3("uCameraTrueUp", self.camera.true_up)
        set2("uResolution", (width, height))
        set1("uTanHalfFov", math.tan(math.radians(self.camera.fov_y_deg) * 0.5))
        set3("uBoundsMin", self.volume.bounds_min)
        set3("uBoundsMax", self.volume.bounds_max)
        set3("uOrigin", np.asarray(self.volume.origin, dtype=np.float64))
        set3("uSpacing", np.asarray(self.volume.spacing, dtype=np.float64))
        set3("uVolumeShape", np.asarray(self.volume.shape_xyz, dtype=np.float64))
        set1("uValueMin", self.transfer_function.value_range[0])
        set1("uValueMax", self.transfer_function.value_range[1])
        set1("uLutStep", (self.transfer_function.value_range[1] - self.transfer_function.value_range[0]) / max(self.lut.rgba.shape[0] - 1, 1))
        set1i("uLutSize", int(self.lut.rgba.shape[0]))
        set1("uTransferMaxGradient", self.lut.max_gradient_norm)
        set1("uBaseStep", self.config.base_step)
        set1i("uAdaptive", 1 if self.config.adaptive else 0)
        set1i("uMaxRefinement", self.config.max_refinement)
        set1("uGradientThreshold", self.config.gradient_threshold)
        set1("uEarlyThreshold", -1.0 if self.config.early_termination_threshold is None else self.config.early_termination_threshold)
        set1i("uMaxBlocks", int(math.ceil(self.volume.diagonal / self.config.base_step)) + 2)
        set1i("uMaxSubdivisions", 1 << self.config.max_refinement if self.config.adaptive else 1)

    def render(self, width: int | None = None, height: int | None = None) -> GLRenderResult:
        self._assert_open()
        render_width = self.width if width is None else int(width)
        render_height = self.height if height is None else int(height)
        self._ensure_fbo(render_width, render_height)
        GL = self._gl
        glfw = self._glfw
        glfw.make_context_current(self._window)
        try:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbo)
            GL.glViewport(0, 0, render_width, render_height)
            GL.glDisable(GL.GL_DEPTH_TEST)
            GL.glDisable(GL.GL_BLEND)
            GL.glDisable(GL.GL_CULL_FACE)
            GL.glClearColor(0.0, 0.0, 0.0, 0.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            self._set_uniforms(render_width, render_height)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_3D, self._volume_texture)
            GL.glActiveTexture(GL.GL_TEXTURE1)
            GL.glBindTexture(GL.GL_TEXTURE_1D, self._lut_texture)
            GL.glBindVertexArray(self._vao)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
            GL.glBindVertexArray(0)
            GL.glFinish()
            error_code = GL.glGetError()
            if error_code != GL.GL_NO_ERROR:
                raise _gl_error("draw", {"gl_error": hex(int(error_code))})

            raw = np.empty((render_height, render_width, 4), dtype=np.float32)
            GL.glReadPixels(
                0,
                0,
                render_width,
                render_height,
                GL.GL_RGBA,
                GL.GL_FLOAT,
                raw,
            )
            checked_readback_size(raw, render_width, render_height)
            raw = np.flipud(raw).copy()
            rgba_linear = np.ascontiguousarray(raw, dtype=np.float32)
            rgba_srgb = rgba_linear.copy()
            rgba_srgb[..., :3] = linear_to_srgb(rgba_linear[..., :3]).astype(np.float32)
        finally:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
            GL.glBindVertexArray(0)
            GL.glActiveTexture(GL.GL_TEXTURE1)
            GL.glBindTexture(GL.GL_TEXTURE_1D, 0)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_3D, 0)
            GL.glUseProgram(0)

        report = {
            "schema_version": "1.0.0",
            "renderer": "medvol-raycast/0.1.0",
            "backend": "opengl_fbo",
            "status": BusinessCode.OK.value,
            "business_code": BusinessCode.OK.value,
            "outputs": {
                "width": render_width,
                "height": render_height,
                "dtype": "float32",
                "layout": "RGBA",
                "linear_color_space": "linear-sRGB",
                "row_order": "top-to-bottom",
                "srgb_selected": self.config.srgb,
                "rgba_linear_sha256": _sha256_float32(rgba_linear),
                "rgba_srgb_sha256": _sha256_float32(rgba_srgb),
            },
            "opengl": {
                "version": self._gl_version,
                "renderer": self._gl_renderer,
                "fbo_complete": True,
                "color_attachment_internal_format": "RGBA32F",
                "volume_internal_format": "R16UI",
                "transfer_internal_format": "RGBA32F",
                "readback_format": "RGBA/FLOAT",
                "readback_bytes": int(rgba_linear.nbytes),
            },
            "config": {
                "sampling": self.config.metadata(),
                "camera": self.camera.metadata(),
                "volume": self.volume.metadata(),
                "transfer_function": self.transfer_function.metadata(),
                "determinism": {
                    "random_seed": self.config.random_seed,
                    "jitter_enabled": False,
                    "fragment_invocations": "fixed 1x1 pixel centers, no randomization",
                    "hash_covers": "float32 RGBA linear buffer in C order",
                },
            },
            "integration": {
                "fbo_texture_available": True,
                "color_texture_id": int(self._color_texture),
                "context_scope": "texture id is valid only while this context is current",
                "readback_array": "owned NumPy float32 RGBA, top-to-bottom",
            },
            "reference_error_analysis": None,
        }
        return GLRenderResult(rgba_linear, rgba_srgb, report, BusinessCode.OK)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        GL = self._gl
        glfw = self._glfw
        window = self._window
        if GL is not None and (window is not None):
            try:
                glfw.make_context_current(window)
            except Exception:
                pass
            for kind, identifier in list(self._live_objects):
                try:
                    if kind in {"fbo", "program", "vao", "volume_texture", "lut_texture", "color_texture"}:
                        if kind == "fbo":
                            GL.glDeleteFramebuffers(1, [identifier])
                        elif kind == "program":
                            GL.glDeleteProgram(identifier)
                        elif kind == "vao":
                            GL.glDeleteVertexArrays(1, [identifier])
                        else:
                            GL.glDeleteTextures(1, [identifier])
                    self._untrack(kind, identifier)
                except Exception:
                    # Preserve destruction progress if the driver has already torn down.
                    pass
            self._fbo = 0
            self._color_texture = 0
            self._program = 0
            self._vao = 0
            self._volume_texture = 0
            self._lut_texture = 0
        try:
            if glfw is not None and window is not None:
                glfw.destroy_window(window)
        finally:
            self._window = None
            if glfw is not None:
                try:
                    glfw.terminate()
                except Exception:
                    pass

    def __enter__(self) -> "OffscreenGLRenderer":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
