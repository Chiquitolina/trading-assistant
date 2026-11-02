"""Presentación humana de las 35 variables HLL, conservando sus IDs exactos.

La orientación es una guía contextual de lectura, NO participa en la distancia
estructural, no es un score ni una señal de trading.
"""
from __future__ import annotations

import hll_structural_fingerprint_v1 as fp

# ID técnico -> (nombre humano, lectura contextual)
LABELS = {
 'geo_low_up_share_96': ('Proporción de mínimos ascendentes · 96 velas', 'up'),
 'geo_high_up_share_96': ('Proporción de máximos ascendentes · 96 velas', 'up'),
 'geo_low_progress_last3_atr_96': ('Progreso de los últimos 3 mínimos · ATR', 'up'),
 'geo_high_progress_last3_atr_96': ('Progreso de los últimos 3 máximos · ATR', 'up'),
 'geo_confirmed_pivots_96': ('Cantidad de pivotes confirmados · 96 velas', 'neutral'),
 'geo_leg_amplitude_cv_96': ('Variabilidad del tamaño de impulsos y correcciones', 'down'),
 'rebuild_last_low_step_atr_96': ('Altura ganada entre mínimos de la última reconstrucción', 'up'),
 'rebuild_last_high_step_atr_96': ('Altura ganada entre máximos de la última reconstrucción', 'up'),
 'rebuild_last_correction_fraction_96': ('Fracción corregida del primer impulso', 'down'),
 'rebuild_second_over_first_rise_96': ('Magnitud segunda subida / primera subida', 'neutral'),
 'rebuild_chain_count_96': ('Cadenas de reconstrucción confirmadas · 96 velas', 'neutral'),
 'compress_residual_osc24_atr_96': ('Oscilación residual sin tendencia · últimas 24 velas', 'down'),
 'compress_residual_ratio_24_96': ('Contracción residual · recientes vs anteriores 24', 'down'),
 'compress_downleg_ratio_new_old_96': ('Tamaño de caídas recientes / anteriores', 'down'),
 'compress_downleg_cv_96': ('Irregularidad en el tamaño de las caídas', 'down'),
 'level_close_distance_atr_96': ('Distancia del cierre a la resistencia · ATR', 'neutral'),
 'level_near_share_48_96': ('Permanencia cerca de resistencia · 48 velas', 'up'),
 'level_near_share_24_96': ('Permanencia cerca de resistencia · 24 velas', 'up'),
 'level_approach_rearm_count_96': ('Aproximaciones rearmadas al nivel · 96 velas', 'neutral'),
 'level_above_zone_share_48_96': ('Cierres por encima de resistencia · 48 velas', 'neutral'),
 'level_zone_position_288': ('Posición de resistencia dentro del rango · 288 velas', 'neutral'),
 'level_max_depth_below_48_atr_96': ('Máxima profundidad bajo la resistencia · ATR', 'down'),
 'maturity_first_low_known_age_96': ('Edad conocida del primer mínimo · 96 velas', 'neutral'),
 'maturity_first_low_known_age_288': ('Edad conocida del primer mínimo · 288 velas', 'neutral'),
 'maturity_last_low_known_age_96': ('Velas desde confirmación del último mínimo', 'neutral'),
 'maturity_last3_low_span_bars_96': ('Duración entre los últimos tres mínimos', 'neutral'),
 'maturity_near_tail_streak_bars_96': ('Velas consecutivas cerca de resistencia al final', 'up'),
 'maturity_last_chain_age_bars_96': ('Edad de la última cadena de reconstrucción', 'neutral'),
 'movement_signed_efficiency_24_96': ('Eficiencia direccional · últimas 24 velas', 'up'),
 'movement_signed_efficiency_48_96': ('Eficiencia direccional · últimas 48 velas', 'up'),
 'movement_net_advance_48_atr_96': ('Avance neto · últimas 48 velas · ATR', 'up'),
 'movement_latest_lhl_retention_96': ('Progreso conservado tras última corrección', 'up'),
 'movement_median_lhl_retracement_96': ('Retroceso relativo típico entre pivotes', 'down'),
 'movement_max_close_drawdown_48_atr_96': ('Mayor pérdida desde máximo de cierre · 48 velas', 'down'),
 'movement_up_close_steps_48_96': ('Fracción de cierres que avanzan · 48 velas', 'neutral'),
}

READING = {
    'up': 'Mayor puede favorecer una construcción ascendente; depende del contexto.',
    'down': 'Menor puede favorecer una construcción ordenada; depende del contexto.',
    'neutral': 'Sin dirección favorable universal.',
}


def check_catalog():
    if set(LABELS) != set(fp.FEATURES):
        raise ValueError('Catálogo de nombres humanos desalineado con las 35 variables')
    if any(not name or direction not in READING for name, direction in LABELS.values()):
        raise ValueError('Etiqueta u orientación inválida')
    return True


def label(feature_id):
    return LABELS[feature_id][0]


def dual_label(feature_id):
    """Valor de dos líneas para tablas de Streamlit (no reemplazar ID técnico)."""
    return f'{label(feature_id)}\n↳ {feature_id}'


def reading(feature_id):
    return READING[LABELS[feature_id][1]]


def reading_badge(feature_id):
    return {'up': '↑ Puede favorecer', 'down': '↓ Puede favorecer',
            'neutral': '↔ Contextual'}[LABELS[feature_id][1]]


check_catalog()
