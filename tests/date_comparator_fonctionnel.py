@log_operator_execution
@type_operator(FIELD_DATAFRAME)
def compare_doc_dates_with_project_end(self, other_value):
    """
    Vérifie pour chaque ligne si la date dans 'target' (ex: DOCDAT)
    est supérieure à la date dans 'comparator' (ex: ENDPRO).
    Les deux colonnes doivent contenir des dates au format yyyymmdd.
    Retourne True si DOCDAT > ENDPRO.
    """
    import pandas as pd
    from datetime import datetime

    target = self.replace_prefix(other_value.get("target"))
    comparator = self.replace_prefix(other_value.get("comparator"))

    # Vérification de la présence des colonnes
    if target not in self.value.columns or comparator not in self.value.columns:
        logger.warning(f"Colonne absente : {target} ou {comparator}")
        return pd.Series([None] * len(self.value))

    def to_date(val):
        try:
            return datetime.strptime(str(val), "%Y%m%d").date()
        except Exception:
            try:
                return pd.to_datetime(val, errors="coerce").date()
            except Exception:
                return None

    target_dates = self.value[target].apply(to_date)
    comparator_dates = self.value[comparator].apply(to_date)

    # Si toutes les dates sont None, retourne None pour SKIPPED
    if target_dates.isnull().all() or comparator_dates.isnull().all():
        logger.warning("Toutes les dates sont invalides ou absentes.")
        return pd.Series([None] * len(self.value))

    results = comparator_dates > target_dates 
    # Si une date est None, retourne None (SKIPPED pour cette ligne)
    results = results.where(target_dates.notnull() & comparator_dates.notnull(), None)
    return results