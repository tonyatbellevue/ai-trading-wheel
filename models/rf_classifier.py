"""随机森林分类器 — 方向预测 (UP=1 / FLAT=0 / DOWN=-1)"""
from __future__ import annotations
import os
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit, GridSearchCV
from sklearn.metrics import classification_report
from indicators.feature_builder import FEATURE_COLS, build_features, build_labels
from config import settings
from loguru import logger


class RFClassifier:
    MODEL_FILE = os.path.join(settings.MODEL_STORE, "rf_model.pkl")

    def __init__(self):
        self.pipeline: Pipeline | None = None
        self._is_trained = False

    # ── 训练 ─────────────────────────────────────────────────────────
    def train(self, df: pd.DataFrame) -> dict:
        """
        df: 已经过 prepare() + add_all_indicators() 的 DataFrame
        返回: 评估指标字典
        """
        df = build_features(df)
        df = build_labels(df)
        df = df.dropna(subset=FEATURE_COLS + ["label"])

        X = df[FEATURE_COLS].values
        y = df["label"].values

        # 时序分割（最后 20% 作为验证集）
        split_idx = int(len(X) * 0.8)
        X_train, X_val = X[:split_idx], X[split_idx:]
        y_train, y_val = y[:split_idx], y[split_idx:]

        logger.info(f"训练集: {len(X_train)} 行，验证集: {len(X_val)} 行")

        param_grid = {
            "clf__n_estimators": [100, 200],
            "clf__max_depth":    [5, 10, None],
            "clf__min_samples_leaf": [10, 20],
        }
        base_pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("clf", RandomForestClassifier(random_state=42, n_jobs=-1, class_weight="balanced")),
        ])
        tscv = TimeSeriesSplit(n_splits=3)
        gs = GridSearchCV(base_pipeline, param_grid, cv=tscv,
                          scoring="f1_weighted", n_jobs=-1, verbose=0)
        gs.fit(X_train, y_train)

        self.pipeline = gs.best_estimator_
        self._is_trained = True

        y_pred = self.pipeline.predict(X_val)
        report = classification_report(y_val, y_pred,
                                       target_names=["DOWN", "FLAT", "UP"],
                                       labels=[-1, 0, 1],
                                       output_dict=True)
        logger.info(f"最佳参数: {gs.best_params_}")
        logger.info(f"验证集加权 F1: {report['weighted avg']['f1-score']:.3f}")

        self.save()
        return report

    # ── 推理 ─────────────────────────────────────────────────────────
    def predict(self, df: pd.DataFrame) -> tuple[int, float]:
        """
        对最近一行数据预测方向和置信度。
        返回: (direction: -1/0/1, confidence: 0.0-1.0)
        """
        if not self._is_trained:
            self.load()
        df = build_features(df)
        row = df[FEATURE_COLS].dropna().iloc[[-1]]
        if row.empty:
            return 0, 0.0
        proba = self.pipeline.predict_proba(row)[0]
        classes = self.pipeline.classes_
        best_idx = int(np.argmax(proba))
        return int(classes[best_idx]), float(proba[best_idx])

    # ── 持久化 ───────────────────────────────────────────────────────
    def save(self):
        os.makedirs(settings.MODEL_STORE, exist_ok=True)
        joblib.dump(self.pipeline, self.MODEL_FILE)
        logger.info(f"模型已保存: {self.MODEL_FILE}")

    def load(self):
        if not os.path.exists(self.MODEL_FILE):
            raise FileNotFoundError(f"模型文件不存在，请先运行 train_model.py: {self.MODEL_FILE}")
        self.pipeline = joblib.load(self.MODEL_FILE)
        self._is_trained = True
        logger.info(f"模型已加载: {self.MODEL_FILE}")
