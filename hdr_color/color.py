"""Chromaticity-derived RGB matrices, Bradford adaptation and gamut checks."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from .errors import BusinessError


ALLOWED_GAMUTS = (
    "BT709",
    "DISPLAY_P3",
    "BT2020",
    "ACES_AP0",
    "ACES_AP1",
)

GAMUT_ALIASES = {
    "REC709": "BT709",
    "BT.709": "BT709",
    "REC2020": "BT2020",
    "BT.2020": "BT2020",
    "P3": "DISPLAY_P3",
    "P3_D65": "DISPLAY_P3",
    "AP0": "ACES_AP0",
    "AP1": "ACES_AP1",
}


@dataclass(frozen=True)
class Gamut:
    name: str
    primaries: tuple[tuple[float, float], ...]
    white: tuple[float, float]


WHITE_POINTS = {
    "D65": (0.3127, 0.3290),
    "D60": (0.32168, 0.33767),
    "D50": (0.3457, 0.3585),
    "DCI": (0.3140, 0.3510),
    "E": (0.3333333333333333, 0.3333333333333333),
}


GAMUTS = {
    "BT709": Gamut(
        "BT709",
        ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06)),
        WHITE_POINTS["D65"],
    ),
    "DISPLAY_P3": Gamut(
        "DISPLAY_P3",
        ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060)),
        WHITE_POINTS["D65"],
    ),
    "BT2020": Gamut(
        "BT2020",
        ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046)),
        WHITE_POINTS["D65"],
    ),
    "ACES_AP0": Gamut(
        "ACES_AP0",
        ((0.73470, 0.26530), (0.00000, 1.00000), (0.00010, -0.07700)),
        WHITE_POINTS["D60"],
    ),
    "ACES_AP1": Gamut(
        "ACES_AP1",
        ((0.71300, 0.29300), (0.16500, 0.83000), (0.12800, 0.04400)),
        WHITE_POINTS["D60"],
    ),
}


BRADFORD = (
    (0.8951, 0.2664, -0.1614),
    (-0.7502, 1.7135, 0.0367),
    (0.0389, -0.0685, 1.0296),
)


Matrix3 = tuple[tuple[float, float, float], ...]
Vector3 = tuple[float, float, float]


def canonical_gamut(value: object) -> str:
    if not isinstance(value, str):
        raise BusinessError(
            "E_GAMUT",
            "gamut must be a string",
            {"value": repr(value), "allowed": list(ALLOWED_GAMUTS)},
        )
    candidate = GAMUT_ALIASES.get(value.strip().upper(), value.strip().upper())
    if candidate not in GAMUTS:
        raise BusinessError(
            "E_GAMUT",
            "unsupported colour gamut",
            {"value": value, "allowed": list(ALLOWED_GAMUTS)},
        )
    return candidate


def resolve_white_point(
    value: object, default_name: str | None = None
) -> tuple[str, tuple[float, float]]:
    if value is None:
        if default_name is None:
            raise BusinessError("E_WHITE_POINT", "white_point is required")
        return default_name, WHITE_POINTS[default_name]
    if isinstance(value, str):
        name = value.strip().upper()
        if name not in WHITE_POINTS:
            raise BusinessError(
                "E_WHITE_POINT",
                "unknown named white point",
                {"value": value, "allowed": sorted(WHITE_POINTS)},
            )
        return name, WHITE_POINTS[name]
    if not isinstance(value, dict):
        raise BusinessError(
            "E_WHITE_POINT",
            "white_point must be a name or an {x,y} object",
            {"value": repr(value)},
        )
    x = value.get("x")
    y = value.get("y")
    if not _finite_number(x) or not _finite_number(y):
        raise BusinessError(
            "E_WHITE_POINT",
            "white point x and y must be finite numbers",
            {"x": repr(x), "y": repr(y)},
        )
    xf, yf = float(x), float(y)
    if xf <= 0.0 or yf <= 0.0 or xf + yf >= 1.0:
        raise BusinessError(
            "E_WHITE_POINT",
            "white point chromaticity is outside its valid domain",
            {"x": xf, "y": yf},
        )
    name = value.get("name")
    label = name if isinstance(name, str) and name else "CUSTOM"
    return label, (xf, yf)


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def chromaticity_to_xyz(xy: tuple[float, float]) -> Vector3:
    x, y = xy
    return (x / y, 1.0, (1.0 - x - y) / y)


def mat_vec(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> Vector3:
    return tuple(
        float(row[0] * vector[0] + row[1] * vector[1] + row[2] * vector[2])
        for row in matrix
    )


def mat_mul(left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]) -> Matrix3:
    return tuple(
        tuple(
            float(
                left[row][0] * right[0][column]
                + left[row][1] * right[1][column]
                + left[row][2] * right[2][column]
            )
            for column in range(3)
        )
        for row in range(3)
    )


def invert3(matrix: Sequence[Sequence[float]]) -> Matrix3:
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) < 1e-15:
        raise BusinessError(
            "E_GAMUT",
            "colour matrix is singular",
            {"matrix": [list(row) for row in matrix], "determinant": determinant},
        )
    inverse_determinant = 1.0 / determinant
    return (
        (
            (e * i - f * h) * inverse_determinant,
            (c * h - b * i) * inverse_determinant,
            (b * f - c * e) * inverse_determinant,
        ),
        (
            (f * g - d * i) * inverse_determinant,
            (a * i - c * g) * inverse_determinant,
            (c * d - a * f) * inverse_determinant,
        ),
        (
            (d * h - e * g) * inverse_determinant,
            (b * g - a * h) * inverse_determinant,
            (a * e - b * d) * inverse_determinant,
        ),
    )


def build_rgb_to_xyz(
    primaries: Sequence[tuple[float, float]], white_xy: tuple[float, float]
) -> Matrix3:
    """Construct RGB->XYZ from chromaticities; no matrix constants are embedded."""

    if len(primaries) != 3:
        raise BusinessError("E_GAMUT", "exactly three primaries are required")
    columns = [_chromaticity_column(xy) for xy in primaries]
    primary_matrix = tuple(
        tuple(columns[column][row] for column in range(3))
        for row in range(3)
    )
    scales = mat_vec(invert3(primary_matrix), chromaticity_to_xyz(white_xy))
    return tuple(
        tuple(primary_matrix[row][column] * scales[column] for column in range(3))
        for row in range(3)
    )


def _chromaticity_column(xy: tuple[float, float]) -> Vector3:
    x, y = xy
    if y == 0.0:
        raise BusinessError(
            "E_GAMUT", "primary y chromaticity must not be zero", {"xy": xy}
        )
    return (x / y, 1.0, (1.0 - x - y) / y)


def bradford_adaptation(
    source_white_xy: tuple[float, float], target_white_xy: tuple[float, float]
) -> Matrix3:
    source_cone = mat_vec(BRADFORD, chromaticity_to_xyz(source_white_xy))
    target_cone = mat_vec(BRADFORD, chromaticity_to_xyz(target_white_xy))
    scale = tuple(
        (
            (
                target_cone[0] / source_cone[0],
                0.0,
                0.0,
            ),
            (
                0.0,
                target_cone[1] / source_cone[1],
                0.0,
            ),
            (
                0.0,
                0.0,
                target_cone[2] / source_cone[2],
            ),
        )
    )
    return mat_mul(invert3(BRADFORD), mat_mul(scale, BRADFORD))


def max_abs_matrix_error(
    left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]
) -> float:
    return max(
        abs(left[row][column] - right[row][column])
        for row in range(3)
        for column in range(3)
    )


@dataclass(frozen=True)
class ColorPipeline:
    source_gamut: Gamut
    target_gamut: Gamut
    source_white_name: str
    source_white_xy: tuple[float, float]
    target_white_name: str
    target_white_xy: tuple[float, float]
    source_rgb_to_xyz: Matrix3
    target_rgb_to_xyz: Matrix3
    adaptation: Matrix3
    forward: Matrix3
    backward: Matrix3

    @property
    def target_luma_coefficients(self) -> tuple[float, float, float]:
        return tuple(self.target_rgb_to_xyz[1])

    def convert_rgb(self, source_rgb: Sequence[float]) -> list[float]:
        return [
            float(
                self.forward[row][0] * source_rgb[0]
                + self.forward[row][1] * source_rgb[1]
                + self.forward[row][2] * source_rgb[2]
            )
            for row in range(3)
        ]

    def reverse_rgb(self, target_rgb: Sequence[float]) -> list[float]:
        return [
            float(
                self.backward[row][0] * target_rgb[0]
                + self.backward[row][1] * target_rgb[1]
                + self.backward[row][2] * target_rgb[2]
            )
            for row in range(3)
        ]

    def luminance(self, rgb: Sequence[float]) -> float:
        return float(
            self.target_luma_coefficients[0] * rgb[0]
            + self.target_luma_coefficients[1] * rgb[1]
            + self.target_luma_coefficients[2] * rgb[2]
        )

    def is_out_of_gamut(self, rgb: Sequence[float], tolerance: float = 1e-9) -> bool:
        return any(channel < -tolerance for channel in rgb)

    def desaturate_to_gamut(self, rgb: Sequence[float]) -> tuple[list[float], bool]:
        """Move toward neutral at constant luminance until all channels are >=0."""

        if not self.is_out_of_gamut(rgb):
            return [float(channel) for channel in rgb], False
        y = self.luminance(rgb)
        if y <= 0.0:
            return [0.0, 0.0, 0.0], True
        alpha_limit = 1.0
        for channel in rgb:
            if channel < 0.0:
                denominator = y - channel
                if denominator <= 0.0:
                    return [y, y, y], True
                alpha_limit = min(alpha_limit, y / denominator)
        alpha = max(0.0, alpha_limit * (1.0 - 1e-12))
        result = [y + alpha * (channel - y) for channel in rgb]
        return [max(0.0, channel) for channel in result], True

    def report(self) -> dict[str, object]:
        return {
            "source_gamut": {
                "name": self.source_gamut.name,
                "primaries_xy": [list(item) for item in self.source_gamut.primaries],
                "task_white_point": {
                    "name": self.source_white_name,
                    "xy": list(self.source_white_xy),
                },
                "rgb_to_xyz": [list(row) for row in self.source_rgb_to_xyz],
                "xyz_to_rgb": [list(row) for row in invert3(self.source_rgb_to_xyz)],
            },
            "target_gamut": {
                "name": self.target_gamut.name,
                "primaries_xy": [list(item) for item in self.target_gamut.primaries],
                "task_white_point": {
                    "name": self.target_white_name,
                    "xy": list(self.target_white_xy),
                },
                "rgb_to_xyz": [list(row) for row in self.target_rgb_to_xyz],
                "xyz_to_rgb": [list(row) for row in invert3(self.target_rgb_to_xyz)],
                "luma_coefficients": list(self.target_luma_coefficients),
            },
            "bradford": [list(row) for row in self.adaptation],
            "combined_source_rgb_to_target_rgb": [list(row) for row in self.forward],
            "roundtrip_matrix_max_abs_error": max_abs_matrix_error(
                mat_mul(self.backward, self.forward),
                ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            ),
            "matrix_derived_from_chromaticities": True,
        }

    @classmethod
    def create(
        cls,
        source_gamut_name: str,
        source_white: tuple[str, tuple[float, float]],
        target_gamut_name: str,
        target_white: tuple[str, tuple[float, float]],
    ) -> "ColorPipeline":
        source_gamut = GAMUTS[canonical_gamut(source_gamut_name)]
        target_gamut = GAMUTS[canonical_gamut(target_gamut_name)]
        source_matrix = build_rgb_to_xyz(source_gamut.primaries, source_white[1])
        target_matrix = build_rgb_to_xyz(target_gamut.primaries, target_white[1])
        adaptation = bradford_adaptation(source_white[1], target_white[1])
        forward = mat_mul(invert3(target_matrix), mat_mul(adaptation, source_matrix))
        backward = mat_mul(invert3(source_matrix), mat_mul(invert3(adaptation), target_matrix))
        return cls(
            source_gamut=source_gamut,
            target_gamut=target_gamut,
            source_white_name=source_white[0],
            source_white_xy=source_white[1],
            target_white_name=target_white[0],
            target_white_xy=target_white[1],
            source_rgb_to_xyz=source_matrix,
            target_rgb_to_xyz=target_matrix,
            adaptation=adaptation,
            forward=forward,
            backward=backward,
        )
