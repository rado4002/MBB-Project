"""CSV + Google Sheets export helpers."""
import csv
import io
import streamlit as st
import pandas as pd


def export_csv_button(df: pd.DataFrame, filename: str = "export.csv"):
    """Add a CSV download button for a DataFrame."""
    csv_data = df.to_csv(index=False)
    st.download_button(
        label="📥 Download CSV",
        data=csv_data,
        file_name=filename,
        mime="text/csv",
    )
