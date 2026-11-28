"""趋势项（分段线性拟合）+ 固定阶数自回归残差的组合预测模型。"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

from .models import Forecast, TempoCurve


@dataclass
class CombinedModel:
    breakpoints: np.ndarray
    trend_beta: np.ndarray
    ar_coef: np.ndarray
    residual_sigma: float
    residuals: np.ndarray
    train_size: int


def _trend_design(x: np.ndarray, breakpoints: np.ndarray) -> np.ndarray:
    """分段线性截断幂基：截距、线性项与各断点铰链基。"""
    columns = [np.ones_like(x), x]
    for knot in breakpoints:
        columns.append(np.maximum(0.0, x - knot))
    return np.column_stack(columns)


def fit_combined_model(
    x: np.ndarray, y: np.ndarray, n_breakpoints: int, ar_order: int
) -> CombinedModel:
    if len(x) < max(ar_order + 3, 6):
        raise ValueError("训练样本不足以拟合趋势+AR 组合模型")
    quantiles = np.linspace(0.0, 1.0, n_breakpoints + 2)[1:-1]
    breakpoints = np.quantile(x, quantiles) if quantiles.size else np.array([])
    design = _trend_design(x, breakpoints)
    trend_beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    trend = design @ trend_beta
    residuals = y - trend

    ar_coef = _fit_ar_stationary(residuals, ar_order)
    usable = residuals[ar_order:]
    if len(usable) >= ar_order + 2:
        ar_design = np.column_stack(
            [residuals[ar_order - lag - 1 : len(residuals) - lag - 1]
             for lag in range(ar_order)]
        )
        fitted = ar_design @ ar_coef
        residual_sigma = float(np.std(usable - fitted, ddof=ar_order + 1))
    else:
        residual_sigma = float(np.std(residuals))
    residual_sigma = max(residual_sigma, 1e-6)
    return CombinedModel(
        breakpoints=breakpoints,
        trend_beta=trend_beta,
        ar_coef=ar_coef,
        residual_sigma=residual_sigma,
        residuals=residuals,
        train_size=len(x),
    )


def _fit_ar_stationary(residuals: np.ndarray, ar_order: int) -> np.ndarray:
    """Yule-Walker 自回归系数，必要时向平稳域收缩。

    保证特征根落在单位圆内，避免多步滚动预测发散；样本不足时退化为 0 系数。
    """
    if len(residuals) < ar_order + 4 or ar_order == 0:
        return np.zeros(ar_order)
    centered = residuals - np.mean(residuals)
    autocorr = np.array(
        [
            float(np.dot(centered[: len(centered) - lag], centered[lag:]))
            / len(centered)
            / (float(np.var(centered)) + 1e-12)
            for lag in range(ar_order + 1)
        ]
    )
    autocorr = np.clip(autocorr, -0.999, 0.999)
    toeplitz = np.array(
        [
            [autocorr[abs(i - j)] for j in range(ar_order)]
            for i in range(ar_order)
        ]
    )
    try:
        coef = np.linalg.solve(toeplitz, autocorr[1:])
    except np.linalg.LinAlgError:
        coef, *_ = np.linalg.lstsq(
            toeplitz, autocorr[1:], rcond=None
        )
    for shrink in (1.0, 0.8, 0.5, 0.25, 0.0):
        candidate = coef * shrink
        if _is_stationary(candidate):
            return candidate
    return np.zeros(ar_order)


def _is_stationary(ar_coef: np.ndarray) -> bool:
    if len(ar_coef) == 0:
        return True
    companion = np.diag(np.ones(len(ar_coef) - 1), -1)
    companion[0] = ar_coef
    eigenvalues = np.linalg.eigvals(companion)
    return bool(np.all(np.abs(eigenvalues) < 1.0))


def _psi_weights(ar_coef: np.ndarray, horizon: int) -> np.ndarray:
    """递归计算 AR(p) 多步预测的 psi 权重，用于区间宽度放大。"""
    order = len(ar_coef)
    psi = np.zeros(horizon)
    psi[0] = 1.0
    for step in range(1, horizon):
        psi[step] = sum(
            ar_coef[lag] * psi[step - lag - 1]
            for lag in range(min(order, step))
        )
    return psi


def rolling_one_step(
    x: np.ndarray,
    y: np.ndarray,
    n_breakpoints: int,
    ar_order: int,
    min_train_points: int,
) -> dict[str, np.ndarray]:
    """滚动原点一步预测：每个点仅使用其之前的数据拟合。"""
    indices = np.arange(len(x))
    predicted = np.full(len(x), np.nan)
    sigma = np.full(len(x), np.nan)
    for idx in indices[min_train_points:]:
        model = fit_combined_model(
            x[:idx], y[:idx], n_breakpoints, ar_order
        )
        trend_point = _trend_design(x[idx : idx + 1], model.breakpoints) @ model.trend_beta
        history = model.residuals
        ar_terms = sum(
            model.ar_coef[lag] * history[-lag - 1]
            for lag in range(ar_order)
            if len(history) > lag
        )
        predicted[idx] = trend_point[0] + ar_terms
        sigma[idx] = model.residual_sigma
    return {"index": indices, "predicted": predicted, "sigma": sigma}


def forecast_curve(
    curve: TempoCurve,
    n_breakpoints: int,
    ar_order: int,
    interval_level: float,
    horizon: int,
    min_train_points: int,
    interval_inflation: float = 1.0,
) -> Forecast:
    """在曲线末尾留 horizon 段做多步滚动预测，并给出预测区间。"""
    x = curve.grid_beats
    y = curve.tempo_bpm
    split = max(min_train_points, len(x) - horizon)
    train_x, train_y = x[:split], y[:split]
    model = fit_combined_model(train_x, train_y, n_breakpoints, ar_order)
    rolling = rolling_one_step(
        train_x,
        train_y,
        n_breakpoints,
        ar_order,
        min_train_points,
    )
    one_step_errors = train_y - rolling["predicted"]
    one_step_errors = one_step_errors[~np.isnan(one_step_errors)]
    full_rolling = rolling_one_step(
        x, y, n_breakpoints, ar_order, min_train_points
    )
    full_errors = y - full_rolling["predicted"]
    full_errors = full_errors[~np.isnan(full_errors)]
    robust_scale = 1.4826 * np.median(
        np.abs(full_errors - np.median(full_errors))
    )
    empirical_scale = (
        float(np.std(one_step_errors))
        if one_step_errors.size >= 3
        else model.residual_sigma
    )
    empirical_scale = max(
        empirical_scale, model.residual_sigma, float(robust_scale)
    )
    # 多步预测的不确定性随步长累积；用步长平方根放大经验尺度，
    # 并叠加趋势拟合残差，避免在变点附近系统性欠宽。
    trend_fit_error = float(
        np.std(train_y - (_trend_design(train_x, model.breakpoints) @ model.trend_beta))
    )
    horizon_scale = np.sqrt(
        empirical_scale**2 + np.maximum(trend_fit_error - empirical_scale, 0.0) ** 2
    )

    test_x = x[split:]
    test_y = y[split:]
    trend_forecast = _trend_design(test_x, model.breakpoints) @ model.trend_beta
    psi = _psi_weights(model.ar_coef, max(len(test_x), 1))

    residual_history = list(model.residuals)
    point_forecast = np.empty(len(test_x))
    lower = np.empty(len(test_x))
    upper = np.empty(len(test_x))
    degrees_of_freedom = max(ar_order + 2, model.train_size - ar_order - 2)
    z_value = float(
        stats.t.ppf(0.5 + interval_level / 2.0, degrees_of_freedom)
    )
    for step in range(len(test_x)):
        ar_terms = sum(
            model.ar_coef[lag] * residual_history[-lag - 1]
            for lag in range(ar_order)
            if len(residual_history) > lag
        )
        point = float(trend_forecast[step] + ar_terms)
        width = z_value * horizon_scale * np.sqrt(
            float(np.sum(psi[: step + 1] ** 2))
        )
        width *= interval_inflation
        point_forecast[step] = point
        lower[step] = point - width
        upper[step] = point + width
        residual_history.append(test_y[step] - point)

    if len(test_y):
        coverage = float(np.mean((test_y >= lower) & (test_y <= upper)))
    else:
        coverage = np.nan

    model_summary = {
        "trend_breakpoints": model.breakpoints.tolist(),
        "trend_beta": model.trend_beta.tolist(),
        "ar_coef": model.ar_coef.tolist(),
        "residual_sigma": model.residual_sigma,
        "empirical_one_step_sigma": empirical_scale,
        "horizon_sigma": float(horizon_scale),
        "train_points": int(model.train_size),
        "forecast_points": int(len(test_x)),
        "interval_level": interval_level,
        "z_value": z_value,
    }
    return Forecast(
        grid_beats=test_x,
        observed=test_y,
        predicted=point_forecast,
        lower=lower,
        upper=upper,
        interval_level=interval_level,
        empirical_coverage=coverage,
        model_summary=model_summary,
    )
