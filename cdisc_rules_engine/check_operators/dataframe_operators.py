from business_rules.operators import BaseType, type_operator
from typing import Union, Any, List, Tuple
from business_rules.fields import FIELD_DATAFRAME
from business_rules.utils import (
    flatten_list,
    is_valid_date,
    vectorized_is_valid,
    vectorized_is_valid_duration,
    vectorized_is_complete_date,
    vectorized_get_dict_key,
    vectorized_is_in,
    vectorized_case_insensitive_is_in,
    apply_regex,
)
from cdisc_rules_engine.check_operators.helpers import vectorized_compare_dates
# ---------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------
from datetime import datetime
# ---------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------
from cdisc_rules_engine.constants import NULL_FLAVORS
from cdisc_rules_engine.utilities.utils import dates_overlap, parse_date
import numpy as np
import dask.dataframe as dd
import pandas as pd
import re
import operator
from uuid import uuid4
from cdisc_rules_engine.models.dataset.dask_dataset import DaskDataset
from cdisc_rules_engine.models.dataset.pandas_dataset import PandasDataset
from cdisc_rules_engine.models.dataset.dataset_interface import DatasetInterface
from pandas.api.types import is_integer_dtype
from cdisc_rules_engine.services import logger
from functools import wraps
import traceback


def log_operator_execution(func):
    @wraps(func)
    def wrapper(self, other_value, *args, **kwargs):
        try:
            logger.info(f"Starting check operator: {func.__name__}")
            result = func(self, other_value)
            logger.info(f"Completed check operator: {func.__name__}")
            return result
        except Exception as e:
            logger.error(
                f"Error in {func.__name__}: {str(e)}, "
                f"traceback: {traceback.format_exc()}"
            )
            error_message = str(e)
            if isinstance(e, TypeError) and (
                "NoneType" in error_message
                or "None" in error_message
                or any(
                    phrase in error_message
                    for phrase in [
                        "NoneType",
                        "object is None",
                        "'NoneType'",
                        "None has no attribute",
                        "unsupported operand type",
                        "bad operand type",
                        "object is not",
                        "cannot be None",
                    ]
                )
            ):
                return None
            else:
                raise

    return wrapper


class DataframeType(BaseType):

    name = "dataframe"

    def __init__(self, data):
        self.value: DatasetInterface = data["value"]
        self.column_prefix_map = data.get("column_prefix_map", {})
        self.value_level_metadata = data.get("value_level_metadata", [])
        self.column_codelist_map = data.get("column_codelist_map", {})
        self.codelist_term_maps = data.get("codelist_term_maps", [])

    def _assert_valid_value_and_cast(self, value):
        return value

    def _custom_str_conversion(self, x):
        if pd.notna(x):
            if isinstance(x, int):
                return str(x).strip()
            elif isinstance(x, float):
                return f"{x:.0f}" if x.is_integer() else str(x).strip()
        return x

    def convert_string_data_to_lower(self, data):
        if self.value.is_series(data):
            data = data.str.lower()
        else:
            data = data.lower()
        return data

    def replace_prefix(self, value: str) -> Union[str, Any]:
        if isinstance(value, str):
            for prefix, replacement in self.column_prefix_map.items():
                if value.startswith(prefix) and replacement is not None:
                    return value.replace(prefix, replacement, 1)
        return value

    def replace_all_prefixes(self, values: List[str]) -> List[str]:
        for i in range(len(values)):
            values[i] = self.replace_prefix(values[i])
        return values

    def get_comparator_data(self, comparator, value_is_literal: bool = False):
        if value_is_literal:
            return comparator
        else:
            return self.value.get(comparator, comparator)

    @log_operator_execution
    def is_column_of_iterables(self, column):
        return self.value.is_series(column) and (
            isinstance(column.iloc[0], list) or isinstance(column.iloc[0], set)
        )

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def exists(self, other_value):
        target_column = self.replace_prefix(other_value.get("target"))

        def check_row(row):
            return any(target_column in item for item in row if isinstance(item, list))

        column_exists = target_column in self.value.columns
        if column_exists:
            return self.value.convert_to_series([True] * len(self.value))
        else:
            exists_in_nested = self.value.apply(check_row, axis=1).any()
            return self.value.convert_to_series([exists_in_nested] * len(self.value))

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def not_exists(self, other_value):
        return ~self.exists(other_value)

    def _check_equality(
        self,
        row,
        target,
        comparator,
        value_is_literal: bool = False,
        value_is_reference: bool = False,
        case_insensitive: bool = False,
        type_insensitive: bool = False,
    ) -> bool:
        """
        Equality checks work slightly differently for clinical datasets.
        See truth table below:
        Operator       --A         --B         Outcome
        equal_to       "" or null  "" or null  False
        equal_to       "" or null  Populated   False
        equal_to       Populated   "" or null  False
        equal_to       Populated   Populated   A == B
        """
        if value_is_reference:
            dynamic_column_name = row[comparator] if comparator in row else None
            comparison_data = (
                row[dynamic_column_name] if dynamic_column_name in row else None
            )
        else:
            comparison_data = (
                comparator
                if comparator not in row or value_is_literal
                else row[comparator]
            )
        both_null = (comparison_data == "" or comparison_data is None) & (
            row[target] == "" or row[target] is None
        )
        if both_null:
            return False
        if type_insensitive:
            target_val = self._custom_str_conversion(row[target])
            comparison_val = self._custom_str_conversion(comparison_data)
        else:
            target_val = row[target]
            comparison_val = comparison_data
        if case_insensitive:
            target_val = row[target].lower() if row[target] else None
            comparison_val = comparison_data.lower() if comparison_data else None
            return target_val == comparison_val
        return target_val == comparison_val

    def _check_inequality(
        self,
        row,
        target,
        comparator,
        value_is_literal: bool = False,
        value_is_reference: bool = False,
        case_insensitive: bool = False,
        type_insensitive: bool = False,
    ) -> bool:
        """
        Equality checks work slightly differently for clinical datasets.
        See truth table below:
        Operator       --A         --B         Outcome
        not_equal_to   "" or null  "" or null  False
        not_equal_to   "" or null  Populated   True
        not_equal_to   Populated   "" or null  True
        not_equal_to   Populated   Populated   A != B
        """
        if value_is_reference:
            dynamic_column_name = row[comparator] if comparator in row else None
            comparison_data = (
                row[dynamic_column_name] if dynamic_column_name in row else None
            )
        else:
            comparison_data = (
                comparator
                if comparator not in row or value_is_literal
                else row[comparator]
            )
        both_null = (comparison_data == "" or comparison_data is None) & (
            row[target] == "" or row[target] is None
        )
        if both_null:
            return False
        if type_insensitive:
            target_val = self._custom_str_conversion(row[target])
            comparison_val = self._custom_str_conversion(comparison_data)
        else:
            target_val = row[target]
            comparison_val = comparison_data
        if case_insensitive:
            target_val = row[target].lower() if row[target] else None
            comparison_val = comparison_data.lower() if comparison_data else None
            return target_val != comparison_val
        return target_val != comparison_val

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def equal_to(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        value_is_reference = other_value.get("value_is_reference", False)
        type_insensitive = other_value.get("type_insensitive", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        return self.value.apply(
            lambda row: self._check_equality(
                row,
                target,
                comparator,
                value_is_literal,
                value_is_reference,
                type_insensitive=type_insensitive,
            ),
            axis=1,
            meta=(None, "bool"),
        ).reset_index(drop=True)

# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------

    def _check_inclusion(
        self,
        row,
        target,
        comparator,
        value_is_literal: bool = False,
        value_is_reference: bool = False,
        case_insensitive: bool = False,
        type_insensitive: bool = False,
    ) -> bool:
        """
        Vérifie si la valeur de row[target] est incluse dans row[comparator]
        ou dans une valeur littérale selon les flags.
        Suit la même logique stricte que _check_equality.
        """

        # Résoudre la valeur de comparaison
        if value_is_reference:
            dynamic_column_name = row[comparator] if comparator in row else None
            
            comparison_data = (
                row[dynamic_column_name] if dynamic_column_name in row else None
            )
            
        else:
            
            comparison_data = (
                comparator
                if comparator not in row or value_is_literal
                else row[comparator]
            )
        

        # Initialiser les valeurs
        target_val = row[target]
        comparison_val = comparison_data
     

        # Vérifier si les deux sont vides ou null
        both_null = (comparison_val == "" or comparison_val is None) & (
            target_val == "" or target_val is None
        )
        if both_null:
            return False

        # Gestion type_insensitive
        if type_insensitive:
            target_val = self._custom_str_conversion(target_val)
            comparison_val = self._custom_str_conversion(comparison_val)
        else:
            # Convertir en string pour la comparaison d'inclusion
            target_val = str(target_val) if target_val is not None else ""
            comparison_val = str(comparison_val) if comparison_val is not None else ""

        # Gestion case_insensitive
        if case_insensitive:
            target_val = target_val.lower() if target_val else ""
            comparison_val = comparison_val.lower() if comparison_val else ""

        # Ne pas considérer target vide comme "in"
        if not target_val or target_val == "" or target_val.lower() == "none":
            return False

        # Comparaison d'inclusion
        return target_val in comparison_val if comparison_val else False
    # -----------------------------------------------------------------------------------------
    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def value_in_other_column(self, other_value):
        """
        Vérifie si la valeur de la colonne 'target' (par exemple DOCVERSN)
        est incluse dans la colonne 'comparator' (par exemple DOCNAME ou valeur fixe).
        """

        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        value_is_reference = other_value.get("value_is_reference", False)
        case_insensitive = other_value.get("case_insensitive", False)
        type_insensitive = other_value.get("type_insensitive", False)

        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
       

        return self.value.apply(
            lambda row: self._check_inclusion(
                row,
                target,
                comparator,
                value_is_literal,
                value_is_reference,
                case_insensitive=case_insensitive,
                type_insensitive=type_insensitive,
            ),
            axis=1,
            meta=(None, "bool"),
        ).reset_index(drop=True)

    # -----------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------
    # -------------------------------DATE COMPARISON ----------------------------------------------
    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def date_validation_in_name(self, other_value):
        """
        Vérifie si la date de la colonne 'target' (ex: DOCDAT) est inférieure ou égale à la date de la colonne 'comparator' (ex: ENDPRO).
        Les deux colonnes doivent contenir des dates au format char.
        """
        target = self.replace_prefix(other_value.get("target"))      # ex: DOCDAT
        comparator = self.replace_prefix(other_value.get("comparator"))  # ex: ENDPRO

        def check(row):
            date1 = row[target]
            date2 = row[comparator]
            try:
                # Conversion en datetime (gère beaucoup de formats)
                dt1 = pd.to_datetime(date1, errors="coerce")
                dt2 = pd.to_datetime(date2, errors="coerce")
                if pd.isna(dt1) or pd.isna(dt2):
                    return False  # Si une date est invalide, on ne signale pas
                return dt1 <= dt2
            except Exception:
                return False

        return self.value.apply(check, axis=1)

# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------

    
    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def value_not_in_other_column(self, other_value):
        """
        Vérifie si la valeur de la colonne 'target' (ex: DOCVERSN)
        n'est PAS incluse dans la colonne 'comparator' (ex: DOCNAME ou valeur fixe).
        """
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        value_is_reference = other_value.get("value_is_reference", False)
        case_insensitive = other_value.get("case_insensitive", False)
        type_insensitive = other_value.get("type_insensitive", False)

        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )

        return self.value.apply(
            lambda row: not self._check_inclusion(
                row,
                target,
                comparator,
                value_is_literal,
                value_is_reference,
                case_insensitive=case_insensitive,
                type_insensitive=type_insensitive,
            ),
            axis=1,
            meta=(None, "bool"),
        ).reset_index(drop=True)
    
    # -----------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------
    
    #CECI MARCHE TRES BIEN 
    # -----------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------
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
    
    
    
    
    
    
    
        # value_is_literal = other_value.get("value_is_literal", False)
        # value_is_reference = other_value.get("value_is_reference", False)
        # case_insensitive = other_value.get("case_insensitive", False)
        # type_insensitive = other_value.get("type_insensitive", False)

        # comparator = (
        #     self.replace_prefix(other_value.get("comparator"))
        #     if not value_is_literal
        #     else other_value.get("comparator")
        # )

        # return self.value.apply(
        #     lambda row: not self._check_inclusion(
        #         row,
        #         target,
        #         comparator,
        #         value_is_literal,
        #         value_is_reference,
        #         case_insensitive=case_insensitive,
        #         type_insensitive=type_insensitive,
        #     ),
        #     axis=1,
        #     meta=(None, "bool"),
        # ).reset_index(drop=True)
    # def check_doc_version_in_name(dataset):
    #     """
    #     Vérifie si la valeur de la colonne DOCVERSN est présente dans DOCNAME.
    #     """
    #     errors = []

    #     if "DOCNAME" not in dataset.columns or "DOCVERSN" not in dataset.columns:
    #         return {"success": True, "errors": []}

    #     for idx, row in dataset.iterrows():
    #         doc_name = str(row["DOCNAME"])
    #         doc_version = str(row["DOCVERSN"])

    #         if doc_version not in doc_name:
    #             errors.append({
    #                 "row": idx + 1,
    #                 "DOCNAME": doc_name,
    #                 "DOCVERSN": doc_version,
    #                 "message": f"Le nom du fichier {doc_name} ne contient pas la version {doc_version}."
    #             })

    #     return {
    #         "success": len(errors) == 0,
    #         "errors": errors
    #     }

 
    # @log_operator_execution
    # @type_operator(FIELD_DATAFRAME)
    # def compare_doc_dates_with_project_end(metadata_df, globvar_df, doc_date_col, endpro_col, datfor_col):
    #     """
    #     Compare les dates de la colonne doc_date_col (ex: DOCDAT) du fichier metadata.xpt
    #     avec la date de fin du projet (endpro_col, ex: ENDPRO) et le format de date (datfor_col, ex: DATFOR)
    #     provenant du fichier globvar.xpt.
    #     """
    #     # Récupère la date de fin du projet et le format de date
    #     endpro_date_str = globvar_df[endpro_col].iloc[0]
    #     date_format_str = globvar_df[datfor_col].iloc[0]

    #     # Convertit la date de fin du projet au format ISO
    #     try:
    #         endpro_date = datetime.strptime(endpro_date_str, date_format_str).date()
    #     except Exception:
    #         # Si le format ne correspond pas, essaie de parser automatiquement
    #         endpro_date = pd.to_datetime(endpro_date_str, errors="coerce").date()

    #     results = []
    #     for idx, row in metadata_df.iterrows():
    #         doc_date_str = row[doc_date_col]
    #         try:
    #             doc_date = datetime.strptime(doc_date_str, date_format_str).date()
    #         except Exception:
    #             doc_date = pd.to_datetime(doc_date_str, errors="coerce").date()
    #         # Comparaison
    #         is_before_or_equal = doc_date <= endpro_date if doc_date and endpro_date else False
    #         results.append({
    #             "row": idx + 1,
    #             doc_date_col: doc_date_str,
    #             endpro_col: endpro_date_str,
    #             "doc_date_iso": doc_date.isoformat() if doc_date else None,
    #             "endpro_iso": endpro_date.isoformat() if endpro_date else None,
    #             "result": is_before_or_equal
    #         })
    #     return results
    
    # @log_operator_execution
    # @type_operator(FIELD_DATAFRAME)
    # def compare_doc_dates_with_project_end(self, other_value):
    #     """
    #     Compare les dates de la colonne doc_date_col (ex: DOCDAT) du fichier metadata.xpt
    #     avec la date de fin du projet (endpro_col, ex: ENDPRO) et le format de date (datfor_col, ex: DATFOR)
    #     provenant du fichier globvar.xpt.
    #     """
    #     # Récupère les paramètres
    #     doc_date_col = other_value.get("doc_date_col")  # ex: DOCDAT
    #     endpro_col = other_value.get("endpro_col")      # ex: ENDPRO
    #     datfor_col = other_value.get("datfor_col")      # ex: DATFOR

    #     # Charge le DataFrame globvar
    #     globvar_df = other_value.get("globvar_df")
    #     if globvar_df is None:
    #         globvar_path = other_value.get("globvar_sheet")
    #         # Adapter le chargement selon le format réel (.xpt, .xlsx, .csv)
    #         if globvar_path.endswith(".xlsx"):
    #             globvar_df = pd.read_excel(globvar_path)
    #         elif globvar_path.endswith(".xpt"):
    #             globvar_df = pd.read_sas(globvar_path, format="xport")
    #         else:
    #             globvar_df = pd.read_csv(globvar_path)

    #     # Récupère la date de fin du projet et le format de date
    #     endpro_date_str = globvar_df[endpro_col].iloc[0]
    #     date_format_str = globvar_df[datfor_col].iloc[0]

    #     # Convertit la date de fin du projet au format ISO
    #     try:
    #         endpro_date = datetime.strptime(str(endpro_date_str), date_format_str).date()
    #     except Exception:
    #         endpro_date = pd.to_datetime(endpro_date_str, errors="coerce").date()

    #     # Applique la comparaison sur le DataFrame principal (self.value)
    #     def check(row):
    #         doc_date_str = row[doc_date_col]
    #         try:
    #             doc_date = datetime.strptime(str(doc_date_str), date_format_str).date()
    #         except Exception:
    #             doc_date = pd.to_datetime(doc_date_str, errors="coerce").date()
    #         return doc_date <= endpro_date if doc_date and endpro_date else False

    #     return self.value.apply(check, axis=1)

    # @log_operator_execution
    # @type_operator(FIELD_DATAFRAME)
    # def compare_doc_dates_with_project_end(self, other_value):
    #     """
    #     Compare les dates de la colonne doc_date_col (ex: DOCDAT) du fichier metadata.xpt
    #     avec la date de fin du projet (endpro_col, ex: ENDPRO) et le format de date (datfor_col, ex: DATFOR)
    #     provenant du fichier globvar.xpt.
    #     Retourne une Series booléenne pour chaque ligne du DataFrame principal.
    #     """

    #     params = other_value.get("params", other_value)
    #     doc_date_col = params.get("doc_date_col")
    #     endpro_col = params.get("endpro_col")
    #     datfor_col = params.get("datfor_col")
    #     globvar_sheet = params.get("globvar_sheet")
    #     # print("parameters:", params)
    #     # print("doc_date_col:", doc_date_col)
    #     # print("endpro_col:", endpro_col)
    #     # print("datfor_col:", datfor_col)
    #     print("globvar_sheet:", globvar_sheet)
        
    #     # Chargement du DataFrame globvar (supporte .xpt, .xlsx, .csv)
    #     if globvar_sheet.endswith(".xlsx"):
    #         globvar_df = pd.read_excel(globvar_sheet)
    #     elif globvar_sheet.endswith(".xpt"):
    #         globvar_df = pd.read_sas(globvar_sheet, format="xport")
    #     else:
    #         globvar_df = pd.read_csv(globvar_sheet)

    #     endpro_date_str = globvar_df[endpro_col].iloc[0]
    #     date_format_str = globvar_df[datfor_col].iloc[0]

    #     try:
    #         endpro_date = datetime.strptime(str(endpro_date_str), date_format_str).date()
    #     except Exception:
    #         endpro_date = pd.to_datetime(endpro_date_str, errors="coerce").date()

    #     def check(row):
    #         doc_date_str = row[doc_date_col]
    #         try:
    #             doc_date = datetime.strptime(str(doc_date_str), date_format_str).date()
    #         except Exception:
    #             doc_date = pd.to_datetime(doc_date_str, errors="coerce").date()
    #         return doc_date <= endpro_date if doc_date and endpro_date else False

    #     return self.value.apply(check, axis=1)

    # @log_operator_execution
    # @type_operator(FIELD_DATAFRAME)
    # def compare_doc_dates_with_project_end(self, other_value):
    #     """
    #     Vérifie que la date du document (doc_date_col) est antérieure ou égale à la date de fin du projet (ENDPRO)
    #     Le format de date est récupéré dans DATFOR, tous deux lus dans globvar_sheet.
    #     """

    #     doc_date_col = other_value.get("doc_date_col")
    #     endpro_col = other_value.get("endpro_col")
    #     datfor_col = other_value.get("datfor_col")
    #     globvar_sheet = other_value.get("globvar_sheet")
    #     print("globvar_sheet:", globvar_sheet)
    #     # Chargement du DataFrame globvar (supporte .xpt, .xlsx, .csv)
    #     if globvar_sheet.endswith(".xlsx"):
    #         globvar_df = pd.read_excel(globvar_sheet)
    #     elif globvar_sheet.endswith(".xpt"):
    #         globvar_df = pd.read_sas(globvar_sheet, format="xport")
    #     else:
    #         globvar_df = pd.read_csv(globvar_sheet)

    #     # Récupère la dernière ligne (valeurs réelles)
    #     endpro_date_str = str(globvar_df[endpro_col].iloc[-1])
    #     date_format_str = str(globvar_df[datfor_col].iloc[-1])

    #     # Conversion du format SDTM (yyyymmdd) en format Python
    #     format_map = {"yyyymmdd": "%Y%m%d"}
    #     py_date_format = format_map.get(date_format_str.lower(), date_format_str)

    #     try:
    #         endpro_date = datetime.strptime(endpro_date_str, py_date_format).date()
    #     except Exception:
    #         endpro_date = pd.to_datetime(endpro_date_str, errors="coerce").date()

    #     def check(row):
    #         doc_date_str = str(row[doc_date_col])
    #         try:
    #             doc_date = datetime.strptime(doc_date_str, py_date_format).date()
    #         except Exception:
    #             doc_date = pd.to_datetime(doc_date_str, errors="coerce").date()
    #         if not doc_date or not endpro_date:
    #             return False
    #         return doc_date <= endpro_date

    #     return self.value.apply(check, axis=1)

    # -----------------------------------------------------------------------------------------
    # @log_operator_execution
    # @type_operator(FIELD_DATAFRAME)
    # def show_last_row_of_sheet(self, other_value):
    #     """
    #     Affiche et retourne la dernière ligne du fichier passé en paramètre 'sheet_path'.
    #     Utilisation typique : pour vérifier le contenu réel de glob_var.xpt.
    #     """
    #     import pandas as pd

    #     sheet_path = other_value.get("sheet_path")
    #     print("sheet_path:", sheet_path)
    #     if not sheet_path:
    #         logger.error("Paramètre 'sheet_path' manquant.")
    #         return None

    #     # Chargement du fichier selon l'extension
    #     if sheet_path.endswith(".xlsx"):
    #         df = pd.read_excel(sheet_path)
    #     elif sheet_path.endswith(".xpt"):
    #         df = pd.read_sas(sheet_path, format="xport")
    #     else:
    #         df = pd.read_csv(sheet_path)

    #     last_row = df.iloc[-1].to_dict()
    #     logger.info(f"Dernière ligne de {sheet_path} : {last_row}")
    #     print(f"Dernière ligne de {sheet_path} : {last_row}")  # Affichage console

    #     return last_row
    
    
    
    # -----------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------



    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def equal_to_case_insensitive(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        return self.value.apply(
            lambda row: self._check_equality(
                row, target, comparator, value_is_literal, case_insensitive=True
            ),
            axis=1,
        )

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def not_equal_to_case_insensitive(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        return self.value.apply(
            lambda row: self._check_inequality(
                row, target, comparator, value_is_literal, case_insensitive=True
            ),
            axis=1,
        )

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def not_equal_to(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        value_is_reference = other_value.get("value_is_reference", False)
        type_insensitive = other_value.get("type_insensitive", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        return self.value.apply(
            lambda row: self._check_inequality(
                row,
                target,
                comparator,
                value_is_literal,
                value_is_reference,
                type_insensitive=type_insensitive,
            ),
            axis=1,
            meta=(None, "bool"),
        ).reset_index(drop=True)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def suffix_equal_to(self, other_value: dict):
        """
        Checks if target suffix is equal to comparator.
        """
        target: str = self.replace_prefix(other_value.get("target"))
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparator: Union[str, Any] = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        suffix: int = self.replace_prefix(other_value.get("suffix"))
        return self._check_equality_of_string_part(
            target, comparison_data, "suffix", suffix
        )

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def suffix_not_equal_to(self, other_value: dict):
        """
        Checks if target suffix is not equal to comparator.
        """
        return ~self.suffix_equal_to(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def prefix_equal_to(self, other_value: dict):
        """
        Checks if target prefix is equal to comparator.
        """
        target: str = self.replace_prefix(other_value.get("target"))
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparator: Union[str, Any] = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        if comparator == "DOMAIN":
            comparison_data = self.column_prefix_map["--"]
        else:
            comparison_data = self.get_comparator_data(comparator, value_is_literal)
        prefix: int = self.replace_prefix(other_value.get("prefix"))
        return self._check_equality_of_string_part(
            target, comparison_data, "prefix", prefix
        )

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def prefix_not_equal_to(self, other_value: dict):
        """
        Checks if target prefix is not equal to comparator.
        """
        return ~self.prefix_equal_to(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def prefix_is_contained_by(self, other_value: dict):
        """
        Checks if target prefix is contained by the comparator.
        """
        target: str = self.replace_prefix(other_value.get("target"))
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparator: Union[str, Any] = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        prefix_length: int = other_value.get("prefix")
        series_to_validate = self._get_string_part_series(
            "prefix", prefix_length, target
        )
        return self._value_is_contained_by(series_to_validate, comparison_data)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def prefix_is_not_contained_by(self, other_value: dict):
        return ~self.prefix_is_contained_by(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def suffix_is_contained_by(self, other_value: dict):
        """
        Checks if target prefix is equal to comparator.
        """
        target: str = self.replace_prefix(other_value.get("target"))
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparator: Union[str, Any] = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        suffix_length: int = other_value.get("suffix")
        series_to_validate = self._get_string_part_series(
            "suffix", suffix_length, target
        )
        return self._value_is_contained_by(series_to_validate, comparison_data)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def suffix_is_not_contained_by(self, other_value: dict):
        return ~self.suffix_is_contained_by(other_value)

    def _get_string_part_series(self, part_to_validate: str, length: int, target: str):
        if not self.value[target].apply(type).eq(str).all():
            raise ValueError("The operator can't be used with non-string values")

        if part_to_validate == "suffix":
            series_to_validate = self.value[target].str.slice(-length)
        elif part_to_validate == "prefix":
            series_to_validate = self.value[target].str.slice(stop=length)
        else:
            raise ValueError(
                f"Invalid part to validate: {part_to_validate}. \
                    Valid values are: suffix, prefix"
            )
        series_to_validate = series_to_validate.mask(pd.isna(self.value[target]))
        return series_to_validate

    def _value_is_contained_by(self, series, comparison_data):
        if self.is_column_of_iterables(comparison_data):
            results = vectorized_is_in(series, comparison_data)
        else:
            results = series.isin(comparison_data)
        return self.value.convert_to_series(results)

    def _check_equality_of_string_part(
        self,
        target: str,
        comparison_data,
        part_to_validate: str,
        length: int,
    ):
        """
        Checks if the given string part is equal to comparison data.
        """
        series_to_validate = self._get_string_part_series(
            part_to_validate, length, target
        )
        return series_to_validate.eq(comparison_data).astype(bool)

    def _where_less_than(self, target, comparison):
        return np.where(target < comparison, True, False)

    def _where_greater_than(self, target, comparison):
        return np.where(target > comparison, True, False)

    def _where_less_than_or_equal_to(self, target, comparison):
        return np.where(target <= comparison, True, False)

    def _where_greater_than_or_equal_to(self, target, comparison):
        return np.where(target >= comparison, True, False)

    def _to_numeric(self, target, **kwargs):
        return pd.to_numeric(target, **kwargs)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def less_than(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        target_column = self._to_numeric(self.value[target], errors="coerce")
        if self.value.is_series(comparison_data):
            comparison_data = self._to_numeric(comparison_data, errors="coerce")
        results = self._where_less_than(target_column, comparison_data)
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def less_than_or_equal_to(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        target_column = self._to_numeric(self.value[target], errors="coerce")
        if self.value.is_series(comparison_data):
            comparison_data = self._to_numeric(comparison_data, errors="coerce")
        results = self._where_less_than_or_equal_to(target_column, comparison_data)
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def greater_than_or_equal_to(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        target_column = self._to_numeric(self.value[target], errors="coerce")
        if self.value.is_series(comparison_data):
            comparison_data = self._to_numeric(comparison_data, errors="coerce")
        results = self._where_greater_than_or_equal_to(target_column, comparison_data)
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def greater_than(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        target_column = self._to_numeric(self.value[target], errors="coerce")
        if self.value.is_series(comparison_data):
            comparison_data = self._to_numeric(comparison_data, errors="coerce")
        results = self._where_greater_than(target_column, comparison_data)
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def contains(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        if self.is_column_of_iterables(self.value[target]) or isinstance(
            comparison_data, str
        ):
            results = vectorized_is_in(comparison_data, self.value[target])
        elif self.value.is_series(comparison_data):
            results = self._series_is_in(self.value[target], comparison_data)
        else:
            # Handles numeric case. This case should never occur
            results = np.where(self.value[target] == comparison_data, True, False)
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def does_not_contain(self, other_value):
        return ~self.contains(other_value)

    def _series_is_in(self, target, comparison_data):
        return np.where(comparison_data.isin(target), True, False)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def contains_case_insensitive(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        comparator = (
            self.replace_prefix(other_value.get("comparator"))
            if not value_is_literal
            else other_value.get("comparator")
        )
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        comparison_data = self.convert_string_data_to_lower(comparison_data)
        if self.is_column_of_iterables(self.value[target]):
            results = vectorized_case_insensitive_is_in(
                comparison_data, self.value[target]
            )
        elif self.value.is_series(comparison_data):
            results = self._series_is_in(
                self.convert_string_data_to_lower(self.value[target]),
                self.convert_string_data_to_lower(comparison_data),
            )
        else:
            results = vectorized_case_insensitive_is_in(
                comparison_data.lower(), self.value[target]
            )
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def does_not_contain_case_insensitive(self, other_value):
        return ~self.contains_case_insensitive(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_contained_by(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value_is_literal = other_value.get("value_is_literal", False)
        comparator = other_value.get("comparator")
        if isinstance(comparator, str) and not value_is_literal:
            # column name provided
            comparator = self.replace_prefix(comparator)
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        if self.is_column_of_iterables(comparison_data):
            results = vectorized_is_in(self.value[target], comparison_data)
        else:
            results = self.value[target].isin(comparison_data)
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_not_contained_by(self, other_value):
        return ~self.is_contained_by(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_contained_by_case_insensitive(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator", [])
        value_is_literal = other_value.get("value_is_literal", False)
        if isinstance(comparator, list):
            comparator = [val.lower() for val in comparator]
        elif isinstance(comparator, str) and not value_is_literal:
            # column name provided
            comparator = self.replace_prefix(comparator)
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        if self.is_column_of_iterables(comparison_data):
            results = vectorized_case_insensitive_is_in(
                self.value[target].str.lower(), comparison_data
            )
            return self.value.convert_to_series(results)
        elif self.value.is_series(comparison_data):
            results = self.value[target].str.lower().isin(comparison_data.str.lower())
        else:
            results = self.value[target].str.lower().isin(comparison_data)
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_not_contained_by_case_insensitive(self, other_value):
        return ~self.is_contained_by_case_insensitive(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def prefix_matches_regex(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        prefix = other_value.get("prefix")
        converted_strings = self.value[target].map(
            lambda x: self._custom_str_conversion(x)
        )
        results = converted_strings.notna() & converted_strings.astype(str).map(
            lambda x: re.search(comparator, x[:prefix]) is not None
        )
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def not_prefix_matches_regex(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        prefix = other_value.get("prefix")
        converted_strings = self.value[target].map(
            lambda x: self._custom_str_conversion(x)
        )
        results = converted_strings.notna() & ~converted_strings.astype(str).map(
            lambda x: re.search(comparator, x[:prefix]) is not None
        )
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def suffix_matches_regex(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        suffix = other_value.get("suffix")
        converted_strings = self.value[target].map(
            lambda x: self._custom_str_conversion(x)
        )
        results = converted_strings.notna() & converted_strings.astype(str).map(
            lambda x: re.search(comparator, x[-suffix:]) is not None
        )
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def not_suffix_matches_regex(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        suffix = other_value.get("suffix")
        converted_strings = self.value[target].map(
            lambda x: self._custom_str_conversion(x)
        )
        results = converted_strings.notna() & ~converted_strings.astype(str).map(
            lambda x: re.search(comparator, x[-suffix:]) is not None
        )
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def matches_regex(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        converted_strings = self.value[target].map(
            lambda x: self._custom_str_conversion(x)
        )
        results = converted_strings.notna() & converted_strings.astype(str).str.match(
            comparator
        )
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def not_matches_regex(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        converted_strings = self.value[target].map(
            lambda x: self._custom_str_conversion(x)
        )
        results = converted_strings.notna() & ~converted_strings.astype(str).str.match(
            comparator
        )
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def equals_string_part(self, other_value):
        """
        Checks that the values in the target column
        equal the result of parsing the value in the comparison
        column with a regex
        """
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        regex = other_value.get("regex")
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        if isinstance(comparison_data, str):
            parsed_data = apply_regex(regex, comparison_data)
        else:
            parsed_data = comparison_data.str.findall(regex).str[0]
        parsed_id = str(uuid4())
        self.value[parsed_id] = parsed_data
        return self.value.apply(
            lambda row: self._check_equality(row, target, parsed_id, value_is_literal),
            axis=1,
        )

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def does_not_equal_string_part(self, other_value):
        return ~self.equals_string_part(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def starts_with(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        if self.value.is_series(comparison_data):
            # need to convert series to tuple to make startswith operator work correctly
            comparison_data: Tuple[str] = tuple(comparison_data)
        results = self.value[target].str.startswith(comparison_data)
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def ends_with(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        if self.value.is_series(comparison_data):
            # need to convert series to tuple to make endswith operator work correctly
            comparison_data: Tuple[str] = tuple(comparison_data)
        results = self.value[target].str.endswith(comparison_data)
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def has_equal_length(self, other_value: dict):
        """
        Checks that the target length is the same as comparator.
        If comparing two columns (value_is_literal is False), the operator
        compares lengths of values in these columns.
        """
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        if self.value.is_series(comparison_data):
            if is_integer_dtype(comparison_data):
                results = self.value[target].str.len().eq(comparison_data).astype(bool)
            else:
                results = (
                    self.value[target]
                    .str.len()
                    .eq(comparison_data.str.len())
                    .astype(bool)
                )
        else:
            results = self.value[target].str.len().eq(comparator).astype(bool)
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def has_not_equal_length(self, other_value: dict):
        return ~self.has_equal_length(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def longer_than(self, other_value: dict):
        """
        Checks if the target is longer than the comparator.
        If comparing two columns (value_is_literal is False), the operator
        compares lengths of values in these columns.
        """
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        if self.value.is_series(comparison_data):
            if is_integer_dtype(comparison_data):
                results = self.value[target].str.len().gt(comparison_data)
            else:
                results = self.value[target].str.len().gt(comparison_data.str.len())
        else:
            results = self.value[target].str.len().gt(comparison_data)
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def longer_than_or_equal_to(self, other_value: dict):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        if self.value.is_series(comparison_data):
            if is_integer_dtype(comparison_data):
                results = self.value[target].str.len().ge(comparison_data)
            else:
                results = self.value[target].str.len().ge(comparison_data.str.len())
        else:
            results = self.value[target].str.len().ge(comparator)
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def shorter_than(self, other_value: dict):
        return ~self.longer_than_or_equal_to(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def shorter_than_or_equal_to(self, other_value: dict):
        return ~self.longer_than(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def empty(self, other_value: dict):
        target = self.replace_prefix(other_value.get("target"))
        results = np.where(
            self.value[target].isin(NULL_FLAVORS) | pd.isna(self.value[target]),
            True,
            False,
        )
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def empty_within_except_last_row(self, other_value: dict):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        order_by_column: str = self.replace_prefix(other_value.get("ordering"))
        # group all targets by comparator
        if order_by_column:
            ordered_df = self.value.sort_values(by=[comparator, order_by_column])
        else:
            ordered_df = self.value.sort_values(by=[comparator])
        grouped_target = ordered_df.groupby(comparator)[target]
        # validate all targets except the last one
        results = grouped_target.apply(lambda x: x[:-1]).apply(
            lambda x: (
                pd.isna(x).all()
                if isinstance(x, (pd.Series, list))
                else (x in NULL_FLAVORS or pd.isna(x))
            )
        )
        if isinstance(self.value, DaskDataset) and self.value.is_series(results):
            results = results.compute()
        # return values with corresponding indexes from results
        return pd.Series(results.reset_index(level=0, drop=True))

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def non_empty(self, other_value: dict):
        return ~self.empty(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def non_empty_within_except_last_row(self, other_value: dict):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        order_by_column: str = self.replace_prefix(other_value.get("ordering"))
        # group all targets by comparator
        if order_by_column:
            ordered_df = self.value.sort_values(by=[comparator, order_by_column])
        else:
            ordered_df = self.value.sort_values(by=[comparator])
        grouped_target = ordered_df.groupby(comparator)[target]
        # validate all targets except the last one
        results = ~grouped_target.apply(lambda x: x[:-1]).apply(
            lambda x: (
                pd.isna(x).all()
                if isinstance(x, (pd.Series, list))
                else (x in NULL_FLAVORS or pd.isna(x))
            )
        )
        if isinstance(self.value, DaskDataset) and self.value.is_series(results):
            computed_results = results.compute()
            return computed_results.reset_index(level=0, drop=True)

        # return values with corresponding indexes from results
        return pd.Series(results.reset_index(level=0, drop=True))

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def contains_all(self, other_value: dict):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        if isinstance(comparator, list):
            # get column as array of values
            values = flatten_list(self.value, comparator)
        else:
            comparator = self.replace_prefix(comparator)
            values = self.value[comparator].unique()
        return self.value.convert_to_series(
            set(values).issubset(set(self.value[target].unique()))
        )

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def not_contains_all(self, other_value: dict):
        return ~self.contains_all(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def invalid_date(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        results = ~vectorized_is_valid(self.value[target])
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def invalid_duration(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        if other_value.get("negative") is False:
            results = ~vectorized_is_valid_duration(self.value[target], False)
        else:
            results = ~vectorized_is_valid_duration(self.value[target], True)
        return self.value.convert_to_series(results)

    def date_comparison(self, other_value, operator):
        target = self.replace_prefix(other_value.get("target"))
        comparator = self.replace_prefix(other_value.get("comparator"))
        value_is_literal: bool = other_value.get("value_is_literal", False)
        comparison_data = self.get_comparator_data(comparator, value_is_literal)
        component = other_value.get("date_component")
        results = np.where(
            vectorized_compare_dates(
                component, self.value[target], comparison_data, operator
            ),
            True,
            False,
        )
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def date_equal_to(self, other_value):
        return self.date_comparison(other_value, operator.eq)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def date_not_equal_to(self, other_value):
        return self.date_comparison(other_value, operator.ne)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def date_less_than(self, other_value):
        return self.date_comparison(other_value, operator.lt)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def date_less_than_or_equal_to(self, other_value):
        return self.date_comparison(other_value, operator.le)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def date_greater_than_or_equal_to(self, other_value):
        return self.date_comparison(other_value, operator.ge)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def date_greater_than(self, other_value):
        return self.date_comparison(other_value, operator.gt)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_incomplete_date(self, other_value):
        return ~self.is_complete_date(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_complete_date(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        results = vectorized_is_complete_date(self.value[target])
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_inconsistent_across_dataset(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        grouping_cols = []
        if isinstance(comparator, str):
            col_name = self.replace_prefix(comparator)
            if col_name in self.value.columns:
                grouping_cols.append(col_name)
        else:
            for col in comparator:
                col_name = self.replace_prefix(col)
                if col_name in self.value.columns:
                    grouping_cols.append(col_name)
        df_check = self.value[grouping_cols + [target]].copy()
        df_check = df_check.fillna("_NaN_")
        results = pd.Series(True, index=df_check.index)
        for name, group in df_check.groupby(grouping_cols):
            if group[target].nunique() == 1:
                results[group.index] = False
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_unique_set(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        values = [target, comparator]
        target_data = flatten_list(self.value, values)
        target_names = []
        for target_name in target_data:
            target_name = self.replace_prefix(target_name)
            if target_name in self.value.columns:
                target_names.append(target_name)
        target_names = list(set(target_names))
        df_group = self.value[target_names].copy()
        df_group = df_group.fillna("_NaN_")
        group_sizes = df_group.groupby(target_names).size()
        counts = df_group.apply(tuple, axis=1).map(group_sizes)
        results = np.where(counts <= 1, True, False)
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_not_unique_set(self, other_value):
        return ~self.is_unique_set(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_not_unique_relationship(self, other_value):
        """
        Validates one-to-one relationship between
        two columns (target and comparator) against a dataset.
        One-to-one means that a pair of columns can be duplicated
        but its integrity must not be violated:
        one value of target always corresponds to
        one value of comparator.
        Examples:

        Valid dataset:
        STUDYID  STUDYDESC
        1        A
        2        B
        3        C
        1        A
        2        B

        Invalid dataset:
        STUDYID  STUDYDESC
        1        A
        2        A
        3        C
        """
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get("comparator")
        if isinstance(comparator, list):
            comparator = self.replace_all_prefixes(comparator)
        else:
            comparator = self.replace_prefix(comparator)
        # remove repeating rows
        df_without_duplicates: DatasetInterface = self.value[
            [target, comparator]
        ].drop_duplicates()
        # we need to check if ANY of the columns (target or comparator) is duplicated
        duplicated_comparator = df_without_duplicates[comparator].duplicated(keep=False)
        duplicated_target = df_without_duplicates[target].duplicated(keep=False)
        result = self.value.convert_to_series([False] * len(self.value))
        if duplicated_comparator.any():
            duplicated_comparator_values = set(
                df_without_duplicates[duplicated_comparator][comparator]
            )
            result += self.value[comparator].isin(duplicated_comparator_values)
        if duplicated_target.any():
            duplicated_target_values = set(
                df_without_duplicates[duplicated_target][target]
            )
            result += self.value[target].isin(duplicated_target_values)
        return result

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_unique_relationship(self, other_value):
        return ~self.is_not_unique_relationship(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_ordered_set(self, other_value):
        target = self.replace_prefix(other_value.get("target"))
        value = other_value.get("comparator")
        if not isinstance(value, str):
            raise Exception("Comparator must be a single String value")
        return self.value.is_column_sorted_within(value, target)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_not_ordered_set(self, other_value):
        return not self.is_ordered_set(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def non_conformant_value_data_type(self, other_value):
        results = False
        for vlm in self.value_level_metadata:
            results |= self.value.apply(
                lambda row: vlm["filter"](row) and not vlm["type_check"](row), axis=1
            )
        return self.value.convert_to_series(results.values)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def non_conformant_value_length(self, other_value):
        results = False
        for vlm in self.value_level_metadata:
            results |= self.value.apply(
                lambda row: vlm["filter"](row) and not vlm["length_check"](row), axis=1
            )
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def conformant_value_data_type(self, other_value):
        results = False
        for vlm in self.value_level_metadata:
            results |= self.value.apply(
                lambda row: vlm["filter"](row) and vlm["type_check"](row),
                axis=1,
                meta=pd.Series([True, False], dtype=bool),
            ).fillna(False)
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def conformant_value_length(self, other_value):
        results = False
        for vlm in self.value_level_metadata:
            results |= self.value.apply(
                lambda row: vlm["filter"](row) and vlm["length_check"](row), axis=1
            )
        return self.value.convert_to_series(results)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def has_next_corresponding_record(self, other_value: dict):
        """
        The operator ensures that value of target in current row
        is the same as value of comparator in the next row.
        In order to achieve this, we just remove last row from target
        and first row from comparator and compare the resulting contents.
        The result is reported for target.
        """
        target = self.replace_prefix(other_value.get("target"))
        comparator = self.replace_prefix(other_value.get("comparator"))
        group_by_column: str = self.replace_prefix(other_value.get("within"))
        order_by_column: str = self.replace_prefix(other_value.get("ordering"))
        target_columns = [target, comparator, group_by_column, order_by_column]
        ordered_df = self.value[target_columns].sort_values(by=[order_by_column])
        grouped_df = ordered_df.groupby(group_by_column)
        results = grouped_df.apply(
            lambda x: self.compare_target_with_comparator_next_row(
                x, target, comparator
            )
        )
        return self.value.convert_to_series(results.explode().tolist())

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def does_not_have_next_corresponding_record(self, other_value: dict):
        return ~self.has_next_corresponding_record(other_value)

    def compare_target_with_comparator_next_row(
        self, df: DatasetInterface, target: str, comparator: str
    ):
        """
        Compares current row of a target with the next row of comparator.
        We can't
        compare last row of target with the next row of comparator
        because there is no row after the last one.
        """
        target_without_last_row = df[target].drop(df[target].tail(1).index)
        comparator_without_first_row = df[comparator].drop(df[comparator].head(1).index)
        results = np.where(
            target_without_last_row.values == comparator_without_first_row.values,
            True,
            False,
        )
        # we add True at the end as the last row of target has nothing to compare
        # so as to not raise errors or incorrect issues in the report with False or NaN
        return self.value.convert_to_series(
            [
                *results,
                True,
            ]
        ).tolist()

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def present_on_multiple_rows_within(self, other_value: dict):
        """
        The operator ensures that the target is present on multiple rows
        within a group_by column. The dataframe is grouped by a certain column
        and the check is applied to each group.
        """
        target = self.replace_prefix(other_value.get("target"))
        min_count: int = other_value.get("comparator") or 1
        group_by_column = self.replace_prefix(other_value.get("within"))
        grouped = self.value.groupby([group_by_column, target])
        meta = (target, bool)
        results = grouped.apply(
            lambda x: self.validate_series_length(x, target, min_count), meta=meta
        )
        uuid = str(uuid4())
        return self.value.merge(
            results.rename(uuid).reset_index(), on=[group_by_column, target]
        )[uuid]

    def validate_series_length(
        self, data: DatasetInterface, target: str, min_length: int
    ):
        return len(data) > min_length

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def not_present_on_multiple_rows_within(self, other_value: dict):
        return ~self.present_on_multiple_rows_within(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def inconsistent_enumerated_columns(self, other_value: dict):
        """
        Check for inconsistencies in enumerated columns of a DataFrame.

        Starting with the smallest/largest enumeration of the given variable,
        return an error if VARIABLE(N+1) is populated but VARIABLE(N) is not populated.
        Repeat for all variables belonging to the enumeration.
        Note that the initial variable will not have an index (VARIABLE) and
        the next enumerated variable has index 1 (VARIABLE1).
        """
        variable_name: str = self.replace_prefix(other_value.get("target"))
        df = self.value
        pattern = rf"^{re.escape(variable_name)}(\d*)$"
        matching_columns = [col for col in df.columns if re.match(pattern, col)]
        if not matching_columns:
            return pd.Series(
                [False] * len(df)
            )  # Return a series of False values if no matching columns
        sorted_columns = sorted(matching_columns, key=lambda x: (len(x), x))

        def check_inconsistency(row):
            prev_populated = (
                pd.notna(row[sorted_columns[0]]) and row[sorted_columns[0]] != ""
            )
            for i in range(1, len(sorted_columns)):
                curr_col = sorted_columns[i]
                curr_value = row[curr_col]
                if pd.notna(curr_value) and curr_value != "" and not prev_populated:
                    return True
                prev_populated = pd.notna(curr_value) and curr_value != ""
            return False

        return df.apply(check_inconsistency, axis=1)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def references_correct_codelist(self, other_value: dict):
        target: str = self.replace_prefix(other_value.get("target"))
        comparator = self.replace_prefix(other_value.get("comparator"))
        result = self.value.apply(
            lambda row: self.valid_codelist_reference(row[target], row[comparator]),
            axis=1,
        )
        return result

    @type_operator(FIELD_DATAFRAME)
    def does_not_reference_correct_codelist(self, other_value: dict):
        return ~self.references_correct_codelist(other_value)

    def next_column_exists_and_previous_is_null(self, row) -> bool:
        row.reset_index(drop=True, inplace=True)
        for index in row[
            row.isin(NULL_FLAVORS) | pd.isna(row)
        ].index:  # leaving null values only
            next_position: int = index + 1
            if next_position < len(row) and not (
                pd.isna(row[next_position]) or row[next_position] in NULL_FLAVORS
            ):
                return True
        return False

    def valid_codelist_reference(self, column_name, codelist):
        if column_name in self.column_codelist_map:
            return codelist in self.column_codelist_map[column_name]
        elif self.column_prefix_map:
            # Check for generic versions of variables (i.e --DECOD)
            for key in self.column_prefix_map:
                if column_name.startswith(self.column_prefix_map[key]):
                    generic_column_name = column_name.replace(
                        self.column_prefix_map[key], key, 1
                    )
                    if generic_column_name in self.column_codelist_map:
                        return codelist in self.column_codelist_map.get(
                            generic_column_name
                        )
        return True

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def has_different_values(self, other_value: dict):
        """
        The operator ensures that the target column has different values.
        """
        target: str = self.replace_prefix(other_value.get("target"))
        is_valid: bool = len(self.value[target].unique()) > 1
        return self.value.convert_to_series([is_valid] * len(self.value[target]))

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def has_same_values(self, other_value: dict):
        return ~self.has_different_values(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_ordered_by(self, other_value: dict):
        """
        Checking validity based on target order.
        """
        target: str = self.replace_prefix(other_value.get("target"))
        sort_order: str = other_value.get("order", "asc")
        if sort_order not in ["asc", "dsc"]:
            raise ValueError("invalid sorting order")
        sort_order_bool: bool = sort_order == "asc"
        return (
            self.value[target]
            .eq(
                self.value[target].sort_values(
                    ascending=sort_order_bool, ignore_index=True
                )
            )
            .astype(bool)
        )

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_not_ordered_by(self, other_value: dict):
        return ~self.is_ordered_by(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def value_has_multiple_references(self, other_value: dict):
        """
        Requires a target column and a reference count column whose values
        are a dictionary containing the number of times that value appears.
        """
        target: str = self.replace_prefix(other_value.get("target"))
        reference_count_column: str = self.replace_prefix(other_value.get("comparator"))
        result = np.where(
            vectorized_get_dict_key(
                self.value[reference_count_column], self.value[target]
            )
            > 1,
            True,
            False,
        )
        return self.value.convert_to_series(result)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def value_does_not_have_multiple_references(self, other_value: dict):
        return ~self.value_has_multiple_references(other_value)

    def check_basic_sort_order(self, group, target, comparator, ascending):
        target_values = group[target].tolist()
        comparator_values = group[comparator].tolist()
        is_sorted = pd.Series(True, index=group.index)

        def safe_compare(x, index):
            if pd.isna(x):
                is_sorted[index] = False
                return "9999-12-31" if ascending else "0001-01-01"
            return x

        expected_order = sorted(
            range(len(comparator_values)),
            key=lambda k: safe_compare(comparator_values[k], group.index[k]),
            reverse=not ascending,
        )
        actual_order = sorted(range(len(target_values)), key=lambda k: target_values[k])

        for i, (exp, act) in enumerate(zip(expected_order, actual_order)):
            if exp != act:
                is_sorted.iloc[i] = False

        return is_sorted

    def check_date_overlaps(self, group, target, comparator):
        comparator_values = group[comparator].tolist()
        is_sorted = pd.Series(True, index=group.index)

        for i in range(len(comparator_values) - 1):
            if is_valid_date(comparator_values[i]) and is_valid_date(
                comparator_values[i + 1]
            ):
                date1, prec1 = parse_date(comparator_values[i])
                date2, prec2 = parse_date(comparator_values[i + 1])
                if prec1 != prec2:
                    overlaps, less_precise = dates_overlap(date1, prec1, date2, prec2)
                    if overlaps and date1.startswith(less_precise):
                        is_sorted.iloc[i] = False
                    elif overlaps and date2.startswith(less_precise):
                        is_sorted.iloc[i + 1] = False

        return is_sorted

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def target_is_sorted_by(self, other_value: dict):
        """
        Checking the sort order based on comparators, including date overlap checks
        """
        target: str = self.replace_prefix(other_value.get("target"))
        within: str = self.replace_prefix(other_value.get("within"))
        columns = other_value["comparator"]
        result = pd.Series([True] * len(self.value), index=self.value.index)
        pandas = isinstance(self.value, PandasDataset)
        for col in columns:
            comparator: str = self.replace_prefix(col["name"])
            ascending: bool = col["sort_order"].lower() != "desc"
            na_pos: str = col["null_position"]
            sorted_df = self.value[[target, within, comparator]].sort_values(
                by=[within, comparator], ascending=ascending, na_position=na_pos
            )
            grouped_df = sorted_df.groupby(within)

            # Check basic sort order, remove multiindex from series
            basic_sort_check = grouped_df.apply(
                lambda x: self.check_basic_sort_order(x, target, comparator, ascending)
            )
            if pandas:
                basic_sort_check = basic_sort_check.reset_index(level=0, drop=True)
            else:
                basic_sort_check = basic_sort_check.reset_index(drop=True)
            result = result & basic_sort_check

            # Check date overlaps, remove multiindex from series
            date_overlap_check = grouped_df.apply(
                lambda x: self.check_date_overlaps(x, target, comparator)
            )
            if pandas:
                date_overlap_check = date_overlap_check.reset_index(level=0, drop=True)
            else:
                date_overlap_check = date_overlap_check.reset_index(drop=True)
            result = result & date_overlap_check

            # handle edge case where a dataframe is returned
            if isinstance(result, (pd.DataFrame, dd.DataFrame)):
                if isinstance(result, dd.DataFrame):
                    result = result.compute()
                result = result.squeeze()
        return result

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def target_is_not_sorted_by(self, other_value: dict):
        return ~self.target_is_sorted_by(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def variable_metadata_equal_to(self, other_value: dict):
        """
        Validates the metadata for variables,
        provided in the metadata column, is equal to
        the comparator.
        Ex.
        target: STUDYID
        comparator: "Exp"
        metadata_column: {"STUDYID": "Req", "DOMAIN": "Req"}
        result: False
        """
        target = self.replace_prefix(other_value.get("target"))
        comparator = other_value.get(
            "comparator"
        )  # Assumes the comparator is a value not a column
        metadata_column = self.replace_prefix(other_value.get("metadata"))
        result = np.where(
            vectorized_get_dict_key(self.value[metadata_column], target) == comparator,
            True,
            False,
        )
        return self.value.convert_to_series(result)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def variable_metadata_not_equal_to(self, other_value: dict):
        return ~self.variable_metadata_equal_to(other_value)

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def shares_at_least_one_element_with(self, other_value: dict):
        target: str = self.replace_prefix(other_value.get("target"))
        comparator: str = self.replace_prefix(other_value.get("comparator"))

        def check_shared_elements(row):
            target_set = (
                set(row[target])
                if isinstance(row[target], (list, set))
                else {row[target]}
            )
            comparator_set = (
                set(row[comparator])
                if isinstance(row[comparator], (list, set))
                else {row[comparator]}
            )
            return bool(target_set.intersection(comparator_set))

        return self.value.apply(check_shared_elements, axis=1).any()

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def shares_exactly_one_element_with(self, other_value: dict):
        target: str = self.replace_prefix(other_value.get("target"))
        comparator: str = self.replace_prefix(other_value.get("comparator"))

        def check_exactly_one_shared_element(row):
            target_set = (
                set(row[target])
                if isinstance(row[target], (list, set))
                else {row[target]}
            )
            comparator_set = (
                set(row[comparator])
                if isinstance(row[comparator], (list, set))
                else {row[comparator]}
            )
            return len(target_set.intersection(comparator_set)) == 1

        return self.value.apply(check_exactly_one_shared_element, axis=1).any()

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def shares_no_elements_with(self, other_value: dict):
        target: str = self.replace_prefix(other_value.get("target"))
        comparator: str = self.replace_prefix(other_value.get("comparator"))

        def check_no_shared_elements(row):
            target_set = (
                set(row[target])
                if isinstance(row[target], (list, set))
                else {row[target]}
            )
            comparator_set = (
                set(row[comparator])
                if isinstance(row[comparator], (list, set))
                else {row[comparator]}
            )
            return len(target_set.intersection(comparator_set)) == 0

        return self.value.apply(check_no_shared_elements, axis=1).all()

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_ordered_subset_of(self, other_value: dict):
        target = self.replace_prefix(other_value.get("target"))
        comparator = self.replace_prefix(other_value.get("comparator"))
        missing_columns = set()

        def check_order(row):
            target_list = row[target]
            comparator_list = row[comparator]
            comparator_positions = {col: idx for idx, col in enumerate(comparator_list)}
            positions = []
            for col in target_list:
                if col in comparator_positions:
                    positions.append(comparator_positions[col])
                else:
                    missing_columns.add(col)
                    return False
            return positions == sorted(positions)

        if isinstance(self.value, DaskDataset):
            results = self.value.apply(check_order, axis=1, meta=("check_order", bool))
            results = self.value.convert_to_series(results)
        else:
            results = self.value.apply(check_order, axis=1)
        if missing_columns:
            logger.info(
                f"Columns not found in comparator list {comparator}: {', '.join(sorted(missing_columns))}"
            )
        return results

    @log_operator_execution
    @type_operator(FIELD_DATAFRAME)
    def is_not_ordered_subset_of(self, other_value: dict):
        return ~self.is_ordered_subset_of(other_value)



