import colorcet as cc
import numpy as np
import pandas as pd
from datetime import timedelta
from enum import Enum, IntEnum
from math import log, log10, ceil
from typing import List
from bokeh.io import curdoc
from bokeh.layouts import column, row
from bokeh.models import ColumnDataSource, MultiSelect, Slider, Button, DatetimeTickFormatter, HoverTool, \
    NumberFormatter
from bokeh.models.widgets import Panel, Tabs, RadioButtonGroup, Div, CheckboxButtonGroup, TableColumn, DataTable
from bokeh.plotting import figure
from bokeh.tile_providers import get_provider, Vendors
from pyproj import Transformer
from tornado.escape import to_basestring

TAB_PANE = "TabPane"
BACKGROUND_COLOR = '#F5F5F5'  # grayish bg color
BOUND = 9_400_000  # bound for world map
EPSILON = 0.1  # small number to prevent division by zero
WIDTH = 1000  # width in pixels of big element
DATETIME_TICK_FORMATTER = DatetimeTickFormatter(days=["%Y-%m-%d"], months=["%Y-%m-%d"], years=["%Y-%m-%d"])
# suffixes
total_suff = "cumulative"
delta_suff = 'daily'
raw = 'raw'
trend = 'trend'
rolling = 'rolling'

# urls for hopkins data
base_url = 'https://raw.githubusercontent.com/CSSEGISandData/COVID-19/master/csse_covid_19_data/' \
           'csse_covid_19_time_series/'
confirmed = 'time_series_covid19_confirmed_global.csv'
deaths = 'time_series_covid19_deaths_global.csv'
recovered = 'time_series_covid19_recovered_global.csv'
population = 'data/population.csv'


def load_data_frames():
    print("refresh")
    # load data directly from github to dataframes
    df_confirmed_ = pd.read_csv(f"{base_url}{confirmed}")
    df_deaths_ = pd.read_csv(f"{base_url}{deaths}")
    df_recovered_ = pd.read_csv(f"{base_url}{recovered}")
    df_population_ = pd.read_csv(population, index_col=0)
    case_columns_ = df_confirmed_.columns[4:]
    # get province with most cases for latitutes
    df_coord_ = get_coord_df(df_confirmed_, case_columns_)
    df_confirmed_ = create_additional_columns(case_columns_, df_confirmed_, df_population_)
    df_deaths_ = create_additional_columns(case_columns_, df_deaths_, df_population_)
    df_recovered_ = create_additional_columns(case_columns_, df_recovered_, df_population_)
    return case_columns_, df_coord_, df_confirmed_, df_deaths_, df_recovered_, df_population_


def get_coord_df(df: pd.DataFrame, case_cols: List[str]) -> pd.DataFrame:
    """

    :param df:
    :param case_cols:
    :return:
    """
    idx = df.groupby(['Country/Region'])[case_cols[-1]].transform(max) == df[case_cols[-1]]
    return df[idx][['Country/Region', 'Lat', 'Long']]


def create_additional_columns(case_col: List, df: pd.DataFrame,df_pop: pd.DataFrame) ->pd.DataFrame:
    df = df.groupby(["Country/Region"])[case_col].agg(sum)
    df = df.merge(df_pop, left_index=True, right_index=True)
    daily = df[case_col].diff(axis=1).fillna(0)[case_col[-8:-1]]
    df["last_day"] = daily[daily.columns[-1]]
    df["last_day_per_capita"] = df["last_day"] / (
            df["Population"] / 1e6)
    df["7_day_average_new"] = daily.mean(axis=1)
    df["7_day_average_new_per_capita"] = df["7_day_average_new"] / (
            df["Population"] / 1e6)
    df["total"] = df[case_col[-1]]
    df["total_per_capita"] = df["total"] / (df["Population"] / 1e6)
    return df


case_columns, df_coord, df_confirmed, df_deaths, df_recovered, df_population = load_data_frames()
ws_replacement = '1'

transformer = Transformer.from_crs("epsg:4326", "epsg:3857")
x_coord, y_coord = transformer.transform(df_coord['Lat'].values, df_coord['Long'].values)


def replace_special_chars(x):
    """
    replace some non alphanumeric values with another character
    :param x:
    :return:
    """
    return x.replace(' ', ws_replacement). \
        replace('-', ws_replacement). \
        replace('(', ws_replacement). \
        replace(')', ws_replacement). \
        replace('*', ws_replacement)


def revert_special_chars_replacement(x):
    # reverts all special character to whitespace
    # it is not correct, but okayish workaround use multiple encodings
    return x.replace(ws_replacement, ' ')


# create a constant color for each country
unique_countries = df_confirmed['Country/Region'].unique()
countries = [(x, x) for x in sorted(unique_countries)]
countries_lower_dict = {x.lower(): x for x in unique_countries}
# tooltip seems to have problems with characters which are no ASCII letters. remove them
unique_countries_wo_special_chars = [replace_special_chars(x) for x in unique_countries]
color_dict = dict(zip(unique_countries_wo_special_chars,
                      cc.b_glasbey_bw_minc_20_maxl_70[:len(unique_countries_wo_special_chars)]
                      )
                  )


class Average(Enum):
    MEAN = 1
    MEDIAN = 2


class Scale(str, Enum):
    LINEAR = 'linear'
    LOGARITHMIC = 'log'


class Prefix(IntEnum):
    confirmed = 0
    deaths = 1
    recovered = 2

class Dashboard:

    def __init__(self,
                 active_average: Average = Average.MEAN,
                 active_y_axis_type: Scale = Scale.LINEAR,
                 active_prefix=Prefix.confirmed,   #'confirmed',
                 active_tab=0,
                 active_window_size=7,
                 active_per_capita=False,
                 active_plot_raw=True,
                 active_plot_average=True,
                 active_plot_trend=True,
                 country_list=['Germany'],
                 ):
        # global variables which can be controlled by interactive bokeh elements
        self.active_average = active_average
        self.active_y_axis_type = active_y_axis_type
        self.active_tab = active_tab
        self.active_window_size = active_window_size
        self.active_per_capita = active_per_capita
        self.active_plot_raw = active_plot_raw
        self.active_plot_average = active_plot_average
        self.active_plot_trend = active_plot_trend
        self.layout = None
        self.source = None
        self.top_new_source = ColumnDataSource(data=dict())
        self.top_total_source = ColumnDataSource(data=dict())
        self.world_circle_source = ColumnDataSource(data=dict())
        self.country_list = country_list
        self.active_prefix = active_prefix
        self.active_df = df_confirmed
        if self.active_prefix == Prefix.deaths:
            self.active_df = df_deaths
        elif self.active_prefix == Prefix.recovered:
            self.active_df = df_recovered

    @staticmethod
    def calc_trend(y: pd.Series, window_size: int):
        """
        calculate a trendline (linear interpolation)
        uses the last window of window_size of data, the rest is filled with nan
        :param y: data to calculate the trendline from
        :param window_size: size of the window to calculate the trendline
        :return: numpy array with the last window_size values contain the trendline, values before are np.nan
        """
        x = list(range(0, len(y)))
        z = np.polyfit(x[-window_size:], np.ravel(y.values[-window_size:]), 1)
        p = np.poly1d(z)
        res = np.empty(len(y))
        res[:] = np.nan
        res[-window_size:] = p(x[-window_size:])
        return res

    def get_lines(self, df: pd.DataFrame, country: str, rolling_window: int = 7):
        """
        gets the raw values for a specific country from the given dataframe
        :param df: dataframe to fetch the data from (one out of infected, deaths, recovered)
        :param country: name of the country to get the data for
        :param rolling_window: size of the window for the rolling average
        :return: numpy arrays for daily cases and cumulative cases, both raw and with sliding window averaging
        """
        avg_fun = lambda x: x.mean()
        if self.active_average == Average.MEDIAN:
            avg_fun = lambda x: np.median(x)
        x_date = [pd.to_datetime(case_columns[0]) + timedelta(days=x) for x in range(0, len(case_columns))]
        df_sub = df[df['Country/Region'] == country]
        absolute = df_sub[case_columns].sum(axis=0).to_frame(name='sum')
        absolute_rolling = absolute.rolling(window=rolling_window, axis=0).apply(avg_fun).fillna(0)
        absolute_trend = self.calc_trend(absolute, rolling_window)
        new_cases = absolute.diff(axis=0).fillna(0)
        new_cases_rolling = new_cases.rolling(window=rolling_window, axis=0).apply(avg_fun).fillna(0)
        new_cases_trend = self.calc_trend(new_cases, rolling_window)
        factor = 1
        if self.active_per_capita:
            pop = float(df_population[df_population['Country/Region'] == country]['Population'])
            pop /= 1e6
            factor = 1 / pop
        return x_date, \
               np.ravel(absolute.values) * factor, \
               np.ravel(absolute_rolling.values) * factor, \
               absolute_trend * factor, \
               np.ravel(new_cases.values) * factor, \
               np.ravel(new_cases_rolling.values * factor), \
               new_cases_trend * factor

    def get_dict_from_df(self, df: pd.DataFrame, country_list: List[str], prefix: str):
        """
        returns the needed data in a dict
        :param df: dataframe to fetch the data
        :param country_list: list of countries for which the data should be fetched
        :param prefix: which data should be fetched, confirmed, deaths or recovered (refers to the dataframe)
        :return: dict with for keys
        """
        new_dict = {}
        for country in country_list:
            x_time, absolute_raw, absolute_rolling, absoulte_trend, delta_raw, delta_rolling, delta_trend = \
                self.get_lines(df, country, self.active_window_size)
            country = replace_special_chars(country)
            new_dict[f"{country}_{prefix}_{total_suff}_{raw}"] = absolute_raw
            new_dict[f"{country}_{prefix}_{total_suff}_{rolling}"] = absolute_rolling
            new_dict[f"{country}_{prefix}_{total_suff}_{trend}"] = absoulte_trend
            new_dict[f"{country}_{prefix}_{delta_suff}_{raw}"] = delta_raw
            new_dict[f"{country}_{prefix}_{delta_suff}_{rolling}"] = delta_rolling
            new_dict[f"{country}_{prefix}_{delta_suff}_{trend}"] = delta_trend
            new_dict['x'] = x_time  # list(range(0, len(delta_raw)))
        return new_dict

    @staticmethod
    def generate_tool_tips(selected_keys) -> HoverTool:
        """
        string magic for the tool tips
        :param selected_keys:
        :return:
        """

        tooltips = [(f"{revert_special_chars_replacement(x.split('_')[0])} ({x.split('_')[-1]})",
                     f"@{x}{{(0,0)}}") if x != 'x' else ('Date', '$x{%F}') for x in selected_keys]
        hover = HoverTool(tooltips=tooltips,
                          formatters={'$x': 'datetime'}
                          )
        return hover

    def generate_source(self):
        """
        initialize the data source with Germany
        :return:
        """
        new_dict = self.get_dict_from_df(self.active_df, self.country_list, self.active_prefix.name)
        new_source = ColumnDataSource(data=new_dict)
        return new_source

    def generate_plot(self, source: ColumnDataSource):
        """
        do the plotting based on interactive elements
        :param source: data source with the selected countries and the selected kind of data (confirmed, deaths, or
        recovered)
        :return: the plot layout in a tab
        """
        # global active_y_axis_type, active_tab
        keys = source.data.keys()
        infected_numbers_new = []
        infected_numbers_absolute = []

        for k in keys:
            if f"{delta_suff}_{raw}" in k:
                infected_numbers_new.append(max(source.data[k]))
            elif f"{total_suff}_{raw}" in k:
                infected_numbers_absolute.append(max(source.data[k]))

        max_infected_new = max(infected_numbers_new)
        y_range = (-1, int(max_infected_new * 1.1))
        y_log_max = 1
        if y_range[1] > 0:
            y_log_max = 10 ** ceil(log10(y_range[1]))
        if self.active_y_axis_type == Scale.LOGARITHMIC:
            y_range = (0.1, y_log_max)
        p_new = figure(title=f"{self.active_prefix.name} (new)", plot_height=400, plot_width=WIDTH, y_range=y_range,
                       background_fill_color=BACKGROUND_COLOR, y_axis_type=self.active_y_axis_type)

        max_infected_numbers_absolute = max(infected_numbers_absolute)
        y_range = (-1, int(max_infected_numbers_absolute * 1.1))
        if y_range[1] > 0:
            y_log_max = 10 ** ceil(log10(y_range[1]))
        if self.active_y_axis_type == 'log':
            y_range = (0.1, y_log_max)

        p_absolute = figure(title=f"{self.active_prefix.name} (absolute)", plot_height=400, plot_width=WIDTH,
                            y_range=y_range,
                            background_fill_color=BACKGROUND_COLOR, y_axis_type=self.active_y_axis_type)

        selected_keys_absolute = []
        selected_keys_new = []
        for vals in source.data.keys():
            line_width = 1.5
            if vals == 'x' in vals:
                selected_keys_absolute.append(vals)
                selected_keys_new.append(vals)
                continue
            tokenz = vals.split('_')
            name = f"{revert_special_chars_replacement(tokenz[0])} ({tokenz[-1]})"
            color = color_dict[tokenz[0]]
            line_dash = 'solid'
            alpha = 1
            if raw in vals:
                if self.active_plot_raw:
                    line_dash = 'dashed'
                    alpha = 0.5
                else:
                    continue
            if trend in vals:
                if self.active_plot_trend:
                    line_width = 5
                    alpha = 0.9
                else:
                    continue
            if rolling in vals:
                if not self.active_plot_average:
                    continue

            if total_suff in vals:
                selected_keys_absolute.append(vals)
                p_absolute.line('x', vals, source=source, line_dash=line_dash, color=color, alpha=alpha,
                                line_width=line_width, line_cap='butt', legend_label=name)
            else:
                selected_keys_new.append(vals)
                p_new.line('x', vals, source=source, line_dash=line_dash, color=color, alpha=alpha,
                           line_width=line_width, line_cap='round', legend_label=name)
        p_absolute.legend.location = "top_left"
        p_absolute.legend.click_policy = "hide"
        p_absolute.xaxis.formatter = DATETIME_TICK_FORMATTER
        p_absolute.add_tools(self.generate_tool_tips(selected_keys_absolute))

        p_new.legend.location = "top_left"
        p_new.legend.click_policy = "hide"
        p_new.xaxis.formatter = DATETIME_TICK_FORMATTER
        p_new.add_tools(self.generate_tool_tips(selected_keys_new))

        tab1 = Panel(child=p_new, title=f"{self.active_prefix.name} (daily)")
        tab2 = Panel(child=p_absolute, title=f"{self.active_prefix.name} (cumulative)")
        tabs = Tabs(tabs=[tab1, tab2], name=TAB_PANE)
        if self.layout is not None:
            tabs.active = self.get_tab_pane().active
        return tabs

    def get_tab_pane(self):
        """
        gets the tabs DOM element
        :return: the tab element with the two plots
        """
        return self.layout.select_one(dict(name=TAB_PANE))

    def create_world_map(self):
        """
        draws the fancy world map and do some projection magic
        :return:
        """
        tile_provider = get_provider(Vendors.CARTODBPOSITRON_RETINA)

        tool_tips = [
            ("(x,y)", "($x, $y)"),
            ("country", "@country"),
            ("number", "@num{(0,0)}")

        ]
        world_map = figure(width=WIDTH, height=400, x_range=(-BOUND, BOUND), y_range=(-10_000_000, 12_000_000),
                           x_axis_type="mercator", y_axis_type="mercator", tooltips=tool_tips)
        # world_map.axis.visible = False
        world_map.add_tile(tile_provider)
        self.world_circle_source = ColumnDataSource(
            dict(x=x_coord, y=y_coord, num=self.active_df['total'], sizes=self.active_df['total'].apply(lambda d: ceil(log(d) * 4) if d > 1 else 1),
                 country=self.active_df['Country/Region']))
        world_map.circle(x='x', y='y', size='sizes', source=self.world_circle_source, fill_color="red", fill_alpha=0.8)
        return world_map


    def update_world_map(self):
        self.world_circle_source.data = dict(x=x_coord, y=y_coord, num=self.active_df['total'], sizes=self.active_df['total'].apply(lambda d: ceil(log(d) * 4) if d > 1 else 1),
             country=self.active_df['Country/Region'])

    def update_data(self, attrname, old, new):
        """
        change the
        :param attrname:
        :param old:
        :param new:
        :return:
        """
        self.country_list = new
        self.source.data = self.get_dict_from_df(self.active_df, self.country_list, self.active_prefix.name)
        self.layout.set_select(dict(name=TAB_PANE), dict(tabs=self.generate_plot(self.source).tabs))

    def update_capita(self, new):
        # callback to change between total and per capita numbers
        if new == 0:
            self.active_per_capita = False  # 'total'
        else:
            self.active_per_capita = True  # 'per_capita'
        self.generate_table_new()
        self.generate_table_cumulative()
        self.update_data('', self.country_list, self.country_list)

    def update_scale_button(self, new):
        """
        changes between log and linear y axis
        :param new:
        :return:
        """
        if new == 0:
            self.active_y_axis_type = Scale.LOGARITHMIC
        else:
            self.active_y_axis_type = Scale.LINEAR
        self.update_data('', self.country_list, self.country_list)

    def update_average_button(self, new):
        """
        changes between mean and median averaging
        :param new:
        :return:
        """
        if new == 0:
            self.active_average = Average.MEAN
        else:
            self.active_average = Average.MEDIAN
        self.update_data('', self.country_list, self.country_list)

    def update_shown_plots(self, new):
        """
        updates what lines are shown in the plot
        :param new: active lines list from [0,1,2]
        :return:
        """
        self.active_plot_raw, self.active_plot_average, self.active_plot_trend = False, False, False
        if 0 in new:
            self.active_plot_raw = True
        if 1 in new:
            self.active_plot_average = True
        if 2 in new:
            self.active_plot_trend = True
        # redraw
        self.update_data('', self.country_list, self.country_list)

    def update_data_frame(self, new):
        """
        updates what dataframe is shown in the plots
        :param new: the new datafrome to be shown out of['confirmed','deaths','recovered']
        :return:
        """

        if new == int(Prefix.confirmed):
            self.active_df = df_confirmed
            self.active_prefix = Prefix.confirmed
        elif new == int(Prefix.deaths):
            self.active_df = df_deaths
            self.active_prefix = Prefix.deaths
        else:
            self.active_df = df_recovered
            self.active_prefix = Prefix.recovered
        self.update_world_map()
        self.generate_table_cumulative()
        self.generate_table_new()
        self.update_data('', self.country_list, self.country_list)

    def update_window_size(self, attr, old, new):
        """
        updates the value of the sliding window
        :param attr: attributes not used
        :param old: old sliding window size
        :param new: new sliding window size
        :return: None
        """
        self.active_window_size = new
        self.update_data('', self.country_list, self.country_list)

    def update_tab(self, attr, old, new):
        """
        should update the active tab in plot
        does not always work, we fetch instead the active tab from somewhere else
        thi function is just left there if sometime it works
        :param attr:
        :param old:
        :param new:
        :return:
        """
        print(f"new tab{new}")
        self.active_tab = new




    def generate_table_new(self):
        """
        generates table for daily new
        :return:
        """
        column_avg = "7_day_average_new"
        column_last_day = "last_day"
        if self.active_per_capita:
            column_avg = "7_day_average_new_per_capita"
            column_last_day = "last_day_per_capita"

        current = self.active_df.sort_values(by=[column_avg], ascending=False).head(-1)
        self.top_new_source.data = {
            'name': current['Country/Region'],
            'number_rolling': current[column_avg],
            'number_daily': current[column_last_day],
        }

    def generate_table_cumulative(self):
        """
        generates tables for cumulutive numbers
        :return:
        """
        column = "total"
        if self.active_per_capita:
            column = "total_per_capita"
        current = self.active_df.sort_values(by=[column], ascending=False).head(-1)
        self.top_total_source.data = {
            'name': current['Country/Region'],
            'number': current[column],
        }

    def do_layout(self):
        """
        generates the overall layout by creating all the widgets, buttons etc and arranges
        them in rows and columns
        :return: None
        """
        self.source = self.generate_source()
        tab_plot = self.generate_plot(self.source)
        multi_select = MultiSelect(title="Option (Multiselect Ctrl+Click):", value=self.country_list,
                                   options=countries, height=500)
        multi_select.on_change('value', self.update_data)
        tab_plot.on_change('active', self.update_tab)

        radio_button_group_per_capita = RadioButtonGroup(
            labels=["Total Cases", "Cases per Million"], active=0 if not self.active_per_capita else 1)
        radio_button_group_per_capita.on_click(self.update_capita)
        radio_button_group_scale = RadioButtonGroup(
            labels=["Logarithmic", "Linear"], active=1 if self.active_y_axis_type == Scale.LINEAR else 0)
        radio_button_group_scale.on_click(self.update_scale_button)

        radio_button_group_df = RadioButtonGroup(
            labels=[Prefix.confirmed.name.title(), Prefix.deaths.name.title(), Prefix.recovered.name.title(),],
            active=int(self.active_prefix))
        radio_button_group_df.on_click(self.update_data_frame)
        refresh_button = Button(label="Refresh Data", button_type="default")
        refresh_button.on_click(load_data_frames)
        slider = Slider(start=1, end=30, value=active_window_size, step=1, title="Window Size for rolling average")
        slider.on_change('value', self.update_window_size)
        radio_button_average = RadioButtonGroup(
            labels=["Mean", "Median"], active=0 if active_average == Average.MEAN else 1)
        radio_button_average.on_click(self.update_average_button)
        plot_variables = [self.active_plot_raw, self.active_plot_average, self.active_plot_trend]
        plots_button_group = CheckboxButtonGroup(
            labels=["Raw", "Averaged", "Trend"], active=[i for i, x in enumerate(plot_variables) if x])
        plots_button_group.on_click(self.update_shown_plots)

        world_map = self.create_world_map()
        div = Div(
            text="""Covid-19 Dashboard created by Andreas Weichslgartner in April 2020 with python, bokeh, pandas, numpy, pyproj, and colorcet. Source Code can be found at <a href="https://github.com/weichslgartner/covid_dashboard/">Github</a>.""",
            width=1600, height=10, align='center')
        self.generate_table_cumulative()
        columns = [
            TableColumn(field="name", title="Contry"),
            TableColumn(field="number_rolling", title="daily avg", formatter=NumberFormatter(format="0.")),
            TableColumn(field="number_daily", title="daily raw", formatter=NumberFormatter(format="0."))
        ]
        top_top_14_new_header = Div(
            text="Highest confirmed (daily)",
            align='center')
        top_top_14_new = DataTable(source=self.top_new_source, name="Highest confirmed(daily)", columns=columns,
                                   width=300, height=380)
        self.generate_table_new()
        columns = [
            TableColumn(field="name", title="Contry"),
            TableColumn(field="number", title="confirmed(cumulative)", formatter=NumberFormatter(format="0."))
        ]

        top_top_14_cum_header = Div(
            text="Highest confirmed (cumulative)",
            align='center')
        top_top_14_cum = DataTable(source=self.top_total_source, name="Highest confirmed(cumulative)", columns=columns,
                                   width=300, height=380)
        self.layout = column(
            row(column(tab_plot, world_map),
                column(top_top_14_new_header, top_top_14_new, top_top_14_cum_header, top_top_14_cum),
                column(refresh_button, radio_button_group_df, radio_button_group_per_capita, plots_button_group,
                       radio_button_group_scale, slider, radio_button_average,
                       multi_select),
                width=800),
            div)

        curdoc().add_root(self.layout)
        curdoc().title = "Bokeh Covid-19 Dashboard"


def parse_bool(arguments: dict, key: str, default_val: bool = True) -> bool:
    """
    parses a boolean get value of the key if it is in the dict, returns default_val otherwise
    :param arguments:
    :param key:
    :param default_val:
    :return:
    """
    if key in args and to_basestring(arguments[key][0]).lower() == str(not default_val).lower():
        return not default_val
    return default_val


def parse_int(arguments: dict, key: str, default_val: int = 7) -> int:
    """
    parses an int from arguments dict
    :param arguments:
    :param key:
    :param default_val:
    :return:
    """
    return int(arguments[key][0]) if key in args else default_val





def parse_arguments(args):
    """
    parse get arguments of rest api
    :param args:
    :return:
    """
    args = {k.lower(): v for k, v in args.items()}
    country_list_ = ['Germany']
    if 'country' in args:
        country_list_ = [countries_lower_dict[to_basestring(c).lower()] for c in args['country'] if
                         to_basestring(c).lower() in countries_lower_dict.keys()]
    if len(country_list_) == 0:
        country_list_ = ['Germany']
    active_per_capita = parse_bool(args, 'per_capita', False)
    active_window_size = parse_int(args, 'window_size', 7)
    active_plot_raw = parse_bool(args, 'plot_raw')
    active_plot_average = parse_bool(args, 'plot_average')
    active_plot_trend = parse_bool(args, 'plot_trend')
    active_average = Average.MEDIAN if 'average' in args and to_basestring(
        args['average'][0]).lower() == 'median' else Average.MEAN
    active_y_axis_type = Scale.LOGARITHMIC if 'scale' in args and to_basestring(
        args['scale'][0]).lower() == 'log' else Scale.LINEAR
    active_prefix = Prefix.confirmed
    if 'data' in args:
        val = to_basestring(args['data'][0]).lower()
        if val in  Prefix.deaths.name:
            active_prefix = Prefix.deaths
        elif val in Prefix.deaths.recovereds:
            active_prefix = Prefix.recovered
    return country_list_, active_per_capita, active_window_size, active_plot_raw, active_plot_average, \
           active_plot_trend, active_average, active_y_axis_type, active_prefix

args = curdoc().session_context.request.arguments
country_list_, active_per_capita, active_window_size, active_plot_raw, active_plot_average, \
active_plot_trend, active_average, active_y_axis_type, active_prefix = parse_arguments(args)

dash = Dashboard(country_list=country_list_,
                 active_per_capita=active_per_capita,
                 active_window_size=active_window_size,
                 active_plot_raw=active_plot_raw,
                 active_plot_average=active_plot_average,
                 active_plot_trend=active_plot_trend,
                 active_y_axis_type=active_y_axis_type,
                 active_prefix=active_prefix)
dash.do_layout()
