
import streamlit as st
import pandas as pd
import json
import os
import numpy as np
from pathlib import Path
from itertools import combinations

# --- Configuration ---
CONFIG_PATH = Path("../configs/ahp_constants.json")
RESULTS_DIR = Path("results")
RESULTS_DIR.mkdir(exist_ok=True)

st.set_page_config(page_title="Expert AHP Panel", layout="wide")

# Random Index for CR calculation
RI_DICT = {1: 0, 2: 0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32, 8: 1.41, 9: 1.45, 10: 1.49}

def load_constants():
    if not CONFIG_PATH.exists():
        alt_path = Path("configs/ahp_constants.json")
        if alt_path.exists():
            with open(alt_path, encoding="utf-8") as f:
                return json.load(f)
        return None
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def calculate_priority_and_cr(factors, judgments):
    n = len(factors)
    matrix = np.ones((n, n))
    
    # Fill matrix from judgments
    idx_map = {f: i for i, f in enumerate(factors)}
    for (f1, f2), val in judgments.items():
        i, j = idx_map[f1], idx_map[f2]
        if val > 0: # f2 is more important
            matrix[i, j] = 1 / (val + 1)
            matrix[j, i] = val + 1
        elif val < 0: # f1 is more important
            matrix[i, j] = abs(val) + 1
            matrix[j, i] = 1 / (abs(val) + 1)
        else:
            matrix[i, j] = 1
            matrix[j, i] = 1
            
    # Eigenvalue method
    evals, evecs = np.linalg.eig(matrix)
    max_ev = np.real(evals.max())
    p_vector = np.real(evecs[:, evals.argmax()])
    p_vector = p_vector / p_vector.sum()
    
    ci = (max_ev - n) / (n - 1) if n > 1 else 0
    ri = RI_DICT.get(n, 1.49)
    cr = ci / ri if ri > 0 else 0
    
    return p_vector, cr, matrix

def save_result(expert_name, scenario, land_use, weights, cr, matrix):
    filename = f"{expert_name}_{scenario}_{land_use}.json".replace(" ", "_").replace("/", "_")
    filepath = RESULTS_DIR / filename
    result = {
        "expert": expert_name,
        "scenario": scenario,
        "land_use": land_use,
        "weights": weights,
        "cr": round(float(cr), 4),
        "matrix": matrix.tolist()
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return filepath

# --- UI ---

st.title("🏛️ Экспертная панель: Метод анализа иерархий (AHP)")
st.markdown("""
Пожалуйста, сравните факторы попарно. Выберите, какой фактор важнее в паре и насколько.
Шкала Саати: 1 — равнозначно, 3 — умеренное превосходство, 5 — сильное, 7 — очень сильное, 9 — крайнее.
""")

data = load_constants()

if not data:
    st.error("Не удалось загрузить конфигурацию весов.")
else:
    with st.sidebar:
        st.header("Настройки")
        expert_name = st.text_input("Имя эксперта", value="Эксперт_1")
        
        scenarios = list(data["scenarios"].keys())
        selected_scenario = st.selectbox("Выберите сценарий", scenarios)
        
        land_uses = list(data["scenarios"][selected_scenario].keys())
        selected_lu = st.selectbox("Тип использования", land_uses)
        
        st.markdown("---")
        
        if st.button("💡 Подставить значения по умолчанию", help="Рассчитать суждения на основе весов из ahp_constants.json"):
            default_weights = data["scenarios"][selected_scenario][selected_lu]
            factors = list(default_weights.keys())
            for f1, f2 in combinations(factors, 2):
                w1 = default_weights[f1]
                w2 = default_weights[f2]
                key = f"pair_{selected_scenario}_{selected_lu}_{f1}_{f2}"
                # Map ratio to Saaty scale (-8 to 8)
                if w1 > 0 and w2 > 0:
                    ratio = w2 / w1
                    if ratio > 1:
                        st.session_state[key] = int(min(round(ratio) - 1, 8))
                    elif ratio < 1:
                        st.session_state[key] = int(max(-(round(1/ratio) - 1), -8))
                    else:
                        st.session_state[key] = 0
            st.rerun()

        if st.button("🔄 Сбросить все оценки", type="secondary"):
            for key in list(st.session_state.keys()):
                if key.startswith("pair_"):
                    st.session_state[key] = 0
            st.rerun()

    factors = list(data["scenarios"][selected_scenario][selected_lu].keys())
    pairs = list(combinations(factors, 2))

    st.header(f"Попарное сравнение: {selected_scenario} → {selected_lu}")
    
    judgments = {}
    
    with st.expander("📝 Инструкция по заполнению", expanded=False):
        st.write("""
        1. Ползунок в центре (0) — факторы равнозначны.
        2. Ползунок **влево** — левый фактор важнее.
        3. Ползунок **вправо** — правый фактор важнее.
        4. Шаг ползунка равен 1 (соответствует шкале Саати от 1 до 9).
        """)

    # Display pairs in a grid or list
    for f1, f2 in pairs:
        key = f"pair_{selected_scenario}_{selected_lu}_{f1}_{f2}"
        st.write(f"---")
        col1, col2, col3 = st.columns([3, 6, 3])
        with col1:
            st.markdown(f"**{f1}**")
        with col3:
            st.markdown(f"<div style='text-align: right;'><b>{f2}</b></div>", unsafe_allow_html=True)
        with col2:
            val = st.slider(
                f"Сравнение {f1} vs {f2}",
                min_value=-8,
                max_value=8,
                value=0,
                step=1,
                key=key,
                label_visibility="collapsed"
            )
            judgments[(f1, f2)] = val
            
            # Display readable label for the current value
            if val < 0:
                st.caption(f"⬅️ {f1} в {abs(val)+1} раз(а) важнее")
            elif val > 0:
                st.caption(f"➡️ {f2} в {val+1} раз(а) важнее")
            else:
                st.caption("⚖️ Равнозначно")

    # Calculations
    p_vector, cr, matrix = calculate_priority_and_cr(factors, judgments)
    weights_dict = {f: round(float(w), 3) for f, w in zip(factors, p_vector)}

    # Results Section
    st.markdown("---")
    res_col1, res_col2 = st.columns([1, 1])
    
    with res_col1:
        st.subheader("📊 Итоговые веса")
        df_weights = pd.DataFrame([{"Фактор": f, "Вес": w} for f, w in weights_dict.items()])
        st.bar_chart(df_weights.set_index("Фактор"))
        st.table(df_weights)

    with res_col2:
        st.subheader("📉 Показатель согласованности (CR)")
        
        cr_color = "green" if cr < 0.10 else "red"
        st.markdown(f"### Consistency Ratio: <span style='color:{cr_color}'>{cr:.4f}</span>", unsafe_allow_html=True)
        
        if cr < 0.10:
            st.success("✅ Матрица согласована (CR < 0.10). Результаты достоверны.")
        else:
            st.error("⚠️ Матрица не согласована (CR ≥ 0.10). Пожалуйста, пересмотрите свои оценки для достижения логической последовательности.")
            st.info("💡 Совет: Согласованность нарушается, если, например, A > B, B > C, но C > A.")

    if st.button("💾 Сохранить итоговую матрицу", type="primary", disabled=(cr >= 0.10)):
        path = save_result(expert_name, selected_scenario, selected_lu, weights_dict, cr, matrix)
        st.success(f"Матрица и веса сохранены: {path.name}")
        st.balloons()
        
    st.download_button(
        label="📥 Скачать JSON для интеграции",
        data=json.dumps({
            "scenario": selected_scenario,
            "land_use": selected_lu,
            "weights": weights_dict,
            "cr": cr
        }, indent=2, ensure_ascii=False),
        file_name=f"ahp_{expert_name}_{selected_scenario}.json",
        mime="application/json"
    )

