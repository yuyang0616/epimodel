from uuid import UUID
from collections import namedtuple

import numpy as np
import pandas as pd

import gspread
from oauth2client.client import GoogleCredentials

from tqdm import tqdm
import ergo
from epimodel import RegionDataset, Level, algorithms
from .definition import GleamDefinition


def gsheet_to_df(url: str):
    """
    Export a DataFrame from a Google Sheets tab. The first row is used
    for column names, and index is set equal to the row number for easy
    cross-referencing.
    """
    sheet_url, _, worksheet_id = url.partition("#gid=")
    worksheet_id = int(worksheet_id or 0)

    client = gspread.authorize(GoogleCredentials.get_application_default())
    spreadsheet = client.open_by_url(sheet_url)
    worksheet = get_worksheet_by_id(spreadsheet, worksheet_id)
    values = worksheet.get_all_values()
    return pd.DataFrame(values[1:], columns=values[0], index=range(2, len(values) + 1))


def get_worksheet_by_id(spreadsheet, worksheet_id):
    """ gspread does not provide this function, so I added it """
    for worksheet in spreadsheet.worksheets():
        if worksheet.id == worksheet_id:
            return worksheet
    raise gspread.WorksheetNotFound(f"id {worksheet_id}")


class ScenarioGenerator:
    FIELDS = [
        "Region",
        "Value",
        "Parameter",
        "Start date",
        "End date",
        "Type",
        "Class",
    ]

    def __init__(self, foretold_token=None, progress_bar=True, rds=None):
        self.foretold = ergo.Foretold(foretold_token) if foretold_token else None
        self.progress_bar = progress_bar
        self.rds = rds or RegionDataset.load(
            "epimodel/data/regions.csv", "epimodel/data/regions-gleam.csv"
        )
        algorithms.estimate_missing_populations(rds)

    def create_scenarios_from_gsheet(self, gsheet_url):
        df = gsheet_to_df(gsheet_url).replace({"": None})
        df = df[pd.notnull(df["Parameter"])][self.FIELDS].copy()
        df["Start date"] = df["Start date"].astype("datetime64[D]")
        df["End date"] = df["End date"].astype("datetime64[D]")
        df["Value"] = self._values_to_float(df["Value"])
        df["Region"] = df["Region"].apply(self._get_region_code)
        return ScenarioSet(df, self.rds)

    def _values_to_float(self, values: pd.Series):
        values = values.copy()
        uuid_filter = values.apply(self._is_uuid)
        values[uuid_filter] = [
            self._get_foretold_mean(uuid)
            for uuid in self._progress_bar(
                values[uuid_filter], desc="fetching Foretold distributions"
            )
        ]
        return values.astype("float")

    @staticmethod
    def _is_uuid(value):
        try:
            UUID(value, version=4)
            return True
        except ValueError:
            return False

    def _get_foretold_mean(self, uuid):
        question_distribution = self.foretold.get_question(uuid)
        # Sample the centers of 100 1%-wide quantiles
        qs = np.arange(0.005, 1.0, 0.01)
        ys = np.array([question_distribution.quantile(q) for q in qs])
        mean = np.sum(ys) / len(qs)
        return mean

    def _get_region_code(self, region):
        if pd.isnull(region):
            return None

        # try code first
        if region in self.rds:
            return region

        # If this fails, assume name. Match Gleam regions first.
        matches = self.rds.find_all_by_name(region, levels=tuple(Level))
        if not matches:
            raise Exception(f"No corresponding region found for {region!r}.")
        return matches[0].Code

    def _progress_bar(self, enum, desc=None):
        if self.progress_bar:
            return tqdm(enum, desc=desc)
        return enum


class ScenarioSet:
    def __init__(self, df: pd.DataFrame, rds: RegionDataset):
        self.rds = rds
        self._set_df(df)

    def _set_df(self, df):
        is_package = df.Type == "Countermeasure package"
        is_background = df.Type == "Background condition"
        assert df[~is_package & ~is_background].empty

        self.package_df = df[is_package]
        self.package_classes = set(self.package_df["Class"])

        self.background_df = df[is_background]
        self.background_classes = set(self.background_df["Class"])


    def get_scenario_definitions(self):
        """ Result: { (background_class, package_class): GleamDefinition } """
        # rows with no class are applied to all scenarios
        b_classless_df = self.background_df[pd.isnull(self.background_df["Class"])]
        p_classless_df = self.package_df[pd.isnull(self.package_df["Class"])]

        res = {}
        for bc in self.background_classes:
            for pc in self.package_classes:
                b_df = self.background_df[self.background_df["Class"] == bc]
                p_df = self.package_df[self.package_df["Class"] == pc]
                res[(bc, pc)] = DefinitionGenerator.definition_from_config(
                    # ensure that package exceptions come before background conditions
                    pd.concat([p_df, p_classless_df, b_df, b_classless_df]), self.rds
                )
        return res


class DefinitionGenerator:
    GLOBAL_PARAMETERS = {
        "name": "set_name",
        "id": "set_id",
        "duration": "set_duration",
        "number of runs": "set_run_count",
        "airline traffic": "set_airline_traffic",
        "seasonality": "set_seasonality",
        "commuting time": "set_commuting_time",
    }
    COMPARTMENT_VARIABLES = (
        "beta",
        "epsilon",
        "mu",
        "imu",
    )

    @classmethod
    def definition_from_config(cls, df: pd.DataFrame, rds: RegionDataset):
        return cls(df, rds).definition

    def __init__(self, df: pd.DataFrame, rds: RegionDataset):
        self.definition = GleamDefinition()
        self.rds = rds
        assert len(df.groupby(["Type", "Class"])) <= 2

        self._parse_df(df)

        self.set_global_parameters()
        self.set_global_compartment_variables()
        self.set_exceptions()

        if "name" not in df.Parameter:
            self.definition.set_default_name()

    def _parse_df(self, df: pd.DataFrame):
        is_compartment = df["Parameter"].isin(self.COMPARTMENT_VARIABLES)
        is_multiplier = df["Parameter"].str.contains(" multiplier")
        is_exception = is_compartment & pd.notnull(df["Region"])

        multipliers = self._prepare_multipliers(df[is_multiplier])
        if multipliers:
            df = df.copy()
            for param, multiplier in multipliers:
                df.loc[df["Parameter"] == param, "Value"] *= multiplier

        self.parameters = df[~is_compartment & ~is_multiplier & ~is_exception]
        self.compartments = df[is_compartment & ~is_exception][["Parameter", "Value"]]
        self.exceptions = self._prepare_exceptions(df[is_exception])

    def set_global_parameters(self):
        self._assert_no_duplicate_values(self.parameters)

        for row in self.parameters.itertuples():
            self._set_parameter_from_df_row(row)

    def set_global_compartment_variables(self):
        self._assert_no_duplicate_values(self.compartments)

        for row in self.compartments.itertuples():
            self.definition.set_compartment_variable(*row)

    def set_exceptions(self):
        self.definition.clear_exceptions()
        for row in self.exceptions.itertuples():
            self.definition.add_exception(*row)

    def _prepare_exceptions(self, exceptions: pd.DataFrame) -> pd.DataFrame:
        """
        Group by time period and region set, creating a new df where
        each row corresponds to the Definition.add_exception interface,
        with regions as an array and variables as a dict.
        """
        return (
            exceptions.groupby(["Region", "Start date", "End date"])
            .apply(lambda group: dict(zip(group["Parameter"], group["Value"])))
            .reset_index()
            .groupby([0, "Start date", "End date"])
            .apply(lambda group: [self.rds[reg] for reg in group["Region"]])
            .reset_index.rename(
                columns={
                    0: "variables",
                    1: "regions",
                    "Start date": "start",
                    "End date": "end",
                }
            )[["variables", "regions", "start", "end"]]
        )

    def _prepare_multipliers(self, multipliers: pd.DataFrame) -> dict:
        """ returns a dict of param: multiplier pairs """
        self._assert_no_duplicate_values(multipliers)
        return dict(
            zip(
                multipliers["Parameter"].str.replace(" multiplier", ""),
                multipliers["Value"],
            )
        )

    def _set_parameter_from_df_row(self, row: namedtuple):
        param = row["Parameter"]
        if param == "run dates":
            if pd.notnull(row["Start date"]):
                self.definition.set_start_date(row["Start date"])
            if pd.notnull(row["End date"]):
                self.definition.set_end_date(row["End date"])
        else:
            value = row["Value"]
            getattr(self.definition, self.GLOBAL_PARAMETERS[param])(value)

    def _assert_no_duplicate_values(self, df):
        counts = df.groupby("Parameter")["Value"].count()
        duplicates = list(counts[counts > 1].index)
        if duplicates:
            raise ValueError(
                "Duplicate values passed to a single scenario "
                f"for the following parameters: {duplicates!r}"
            )