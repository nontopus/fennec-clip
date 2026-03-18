from flask import (
    Flask,
    jsonify,
    request,
    render_template,
    send_from_directory,
    send_file,
    Response,
)
from math import radians, cos, sin, asin, sqrt
from collections import defaultdict, Counter
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit
from scipy.interpolate import interp1d
from pymavlink import mavutil
from functools import reduce
from flask_cors import CORS
import simplejson as json
import datetime as dt
import pandas as pd
import numpy as np
import requests
import datetime
import time
import uuid
import math
import pytz
import re
import os

import tempfile

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes
socketio = SocketIO(app, cors_allowed_origins="*")

UPLOAD_FOLDER = "uploads"
DOWNLOAD_FOLDER = "downloads"
current_json_file = None

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

allowed_export_packet_types = [
    "IMU",
    "MAG",
    "RCIN",
    "RCOU",
    "GPS",
    "GPA",
    "BAT",
    "POWR",
    "MCU",
    "BARO",
    "RATE",
    "ATT",
    "VIBE",
    "HELI",
    "MODE",
    "XKF1",
    "XKF5",
]
global_parsed_data = {}

# Initialize DataFrame
df = pd.DataFrame()
current_excel_file = None
current_bin_file = None


import pandas as pd
import os
import time

def save_all_dataframes(data_frames, bin_file_path, output_folder='./temp_df'):
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    base_name = os.path.splitext(os.path.basename(bin_file_path))[0]
    file_path = os.path.join(output_folder, f'{base_name}.pkl')

    # Prepare the data for saving
    prepared_data = {}
    for packet_type, data in data_frames.items():
        if isinstance(data, list):
            prepared_data[packet_type] = pd.DataFrame(data)
        elif isinstance(data, pd.DataFrame):
            prepared_data[packet_type] = data
        else:
            print(f"Skipping {packet_type}: Not a list or DataFrame.")
            continue

    try:
        start_time = time.time()

        with open(file_path, 'wb') as file:
            pd.to_pickle(prepared_data, file)

        end_time = time.time()
        print(f"All DataFrames saved in {end_time - start_time:.2f} seconds to {output_folder}.")
    except Exception as e:
        print(f"Error occurred while saving DataFrames: {e}")


def load_all_dataframes(bin_file_path):
    folder_path = './temp_df'
    base_name = os.path.splitext(os.path.basename(bin_file_path))[0]
    file_path = os.path.join(folder_path, f'{base_name}.pkl')

    if not os.path.exists(file_path):
        print(f"No saved DataFrames found for {bin_file_path}")
        return {}, False

    try:
        start_time = time.time()

        with open(file_path, 'rb') as file:
            data_frames = pd.read_pickle(file)

        end_time = time.time()
        print(f"All DataFrames loaded in {end_time - start_time:.2f} seconds.")
        return data_frames, True
    except Exception as e:
        print(f"Error occurred while loading DataFrames: {e}")
        return {}, False


def haversine_distance(lat1, lon1, lat2, lon2, axis):
    """
    Calculate the north-south (axis='lat') or east-west (axis='lng')
    great circle distance in meters between two points on the earth.
    """
    # Convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

    # Differences in coordinates
    dlat = lat2 - lat1
    dlon = lon2 - lon1

    # Haversine formula components
    a_lat = sin(dlat / 2) ** 2
    a_lon = cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2

    if axis == "lat":
        a = a_lat
    elif axis == "lng":
        a = a_lon
    else:
        raise ValueError("Axis must be 'lat' or 'lng'")

    c = 2 * asin(sqrt(a))

    # Radius of Earth in kilometers is 6371. Convert to meters by multiplying by 1000
    distance_in_meters = 6371 * c * 1000
    return distance_in_meters


# Modified fetch_weather_data function
def fetch_weather_data(start_timestamp, end_timestamp):
    url = f"https://weather.itsnull.net//historic?start-timestamp={int(start_timestamp)}&end-timestamp={int(end_timestamp)}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()["data"], True
    else:
        print(f"Failed to fetch weather data: {response.status_code}")
        return [], False


# The mapping is specific to ArduPilot on helicopters. This info was sourced from ChatGPT-4. YMMV.
def map_mode_number_to_name(mode_number):
    mode_map = {
        0: "STABILIZE",
        1: "ACRO",
        2: "ALT_HOLD",
        3: "AUTO",
        4: "GUIDED",
        5: "LOITER",
        6: "RTL",
        7: "CIRCLE",
        9: "LAND",
        11: "DRIFT",
        13: "SPORT",
        14: "FLIP",
        15: "AUTOTUNE",
        16: "POSHOLD",
        17: "BRAKE",
        18: "THROW",
        19: "AVOID_ADSB",
        20: "GUIDED_NOGPS",
        21: "SMART_RTL",
        22: "FLOWHOLD",
        23: "FOLLOW",
        24: "ZIGZAG",
        25: "SYSTEMID",
        26: "AUTOROTATE",
    }
    return mode_map.get(mode_number, f"Unknown-{mode_number}")


def calculate_unix_epoch_time_for_record(
    time_us, initial_unix_epoch_time, initial_time_us
):
    return initial_unix_epoch_time + (time_us - initial_time_us) / 1e6


def calculate_unix_epoch_time_from_timeus(
    time_us, initial_unix_epoch_time, initial_time_us
):
    delta_time_s = (
        time_us - initial_time_us
    ) / 1e6  # Converting microseconds to seconds
    return initial_unix_epoch_time + delta_time_s


def normalize_gps_data(gps_df):
    """
    Append distinct lat_m and lng_m columns to the GPS DataFrame.
    """
    ref_lat, ref_lon = gps_df.iloc[0]["Lat"], gps_df.iloc[0]["Lng"]

    # Calculate north-south distance (latitude difference)
    gps_df["lat_m"] = gps_df.apply(
        lambda row: haversine_distance(ref_lat, ref_lon, row["Lat"], ref_lon, "lat"),
        axis=1,
    )

    # Calculate east-west distance (longitude difference)
    gps_df["lng_m"] = gps_df.apply(
        lambda row: haversine_distance(ref_lat, ref_lon, ref_lat, row["Lng"], "lng"),
        axis=1,
    )

    return gps_df


def convert_pressure_to_meters(pressure_in_pascals, temperature_celsius=None):
    """
    Convert pressure from Pascals to altitude in meters using the barometric formula.
    If temperature_celsius is None or not provided, defaults to 15 degrees Celsius (ISA standard conditions at sea level).
    """
    # Default to ISA standard sea level temperature if temperature is None or not provided
    temperature_celsius = 15 if temperature_celsius is None else temperature_celsius

    pressure_in_millibars = pressure_in_pascals / 100
    sea_level_pressure = 1013.25  # ISA standard sea level pressure in millibars

    # Convert temperature to Kelvin
    temperature_kelvin = temperature_celsius + 273.15

    # Constants
    R = 8.314462618  # Universal gas constant in J/(mol·K)
    g = 9.80665  # Acceleration due to gravity in m/s^2
    M = 0.0289644  # Molar mass of Earth's air in kg/mol

    # Scale height calculation
    H = R * temperature_kelvin / (g * M)

    # Barometric formula
    altitude = -H * math.log(pressure_in_millibars / sea_level_pressure)
    return altitude


def calculate_average_temperature(weather_data):
    """
    Calculate the average temperature from a list of weather data records.

    The function expects each record in the weather_data list to have a 'temp_avg' key with the temperature in Fahrenheit.
    It calculates the average temperature in Celsius.

    :param weather_data: List of dictionaries, each containing weather data with a 'temp_avg' key in Fahrenheit.
    :return: The average temperature in Celsius, or None if no valid data is found.
    """
    total_temp_celsius = 0
    count = 0
    for record in weather_data:
        if "temp_avg" in record:
            # Convert temperature from Fahrenheit to Celsius
            temp_celsius = (record["temp_avg"] - 32) * 5 / 9
            total_temp_celsius += temp_celsius
            count += 1

    return round(total_temp_celsius / count, 2) if count > 0 else None


def gps_time_to_unix_epoch(gms, gwk):
    gps_epoch = datetime.datetime(1980, 1, 6, 0, 0, 0)
    seconds_since_gps_epoch = gwk * 604800 + gms / 1000
    return (gps_epoch + datetime.timedelta(seconds=seconds_since_gps_epoch)).timestamp()


def determine_key_for_packet(msg_dict):
    packet_type = msg_dict["mavpackettype"]
    instance = msg_dict.get("I", 0)

    if packet_type == "VIBE":
        instance = msg_dict.get("IMU", 0)
    elif packet_type == "MODE":
        mode_name = map_mode_number_to_name(msg_dict.get("Mode", -1))
        msg_dict["ModeName"] = mode_name
    elif "XKF" in packet_type:
        instance = msg_dict.get("C", 0)
        if instance not in [0, 1]:
            instance = 0  # Default to 0 if 'c' is not 0 or 1

    # Create a unique key for each packet type and instance
    if packet_type in ["IMU", "VIBE", "MAG"] or "XKF" in packet_type:
        key = f"{packet_type}_{instance}"
    else:
        key = packet_type

    return key

def resample_to_50hz(input_df, new_time_series, kind='linear'):
    """
    Resample a dataframe to 50Hz using interpolation, ignoring non-numeric columns.

    :param input_df: DataFrame to resample.
    :param new_time_series: The target time series (50Hz), should be a numeric array.
    :param kind: The type of interpolation (default is linear).
    :return: Resampled DataFrame.
    """
    # Ensure the new time series is a numeric array
    new_time_series = np.asarray(new_time_series, dtype=np.float64)

    # Filter out non-numeric columns (excluding 'Unix_Epoch_Time')
    numeric_cols = input_df.select_dtypes(include=[np.number]).columns.tolist()
    if 'Unix_Epoch_Time' in input_df.columns and 'Unix_Epoch_Time' not in numeric_cols:
        numeric_cols.append('Unix_Epoch_Time')

    numeric_df = input_df[numeric_cols]

    # Create a copy to avoid SettingWithCopyWarning
    numeric_df = numeric_df.copy()

    # Ensure Unix_Epoch_Time is numeric
    numeric_df.loc[:, 'Unix_Epoch_Time'] = pd.to_numeric(numeric_df['Unix_Epoch_Time'], errors='coerce')

    # Drop rows with NaN in 'Unix_Epoch_Time' after conversion
    numeric_df = numeric_df.dropna(subset=['Unix_Epoch_Time'])

    # Creating an interpolation function for each numeric column
    interp_funcs = {col: interp1d(numeric_df['Unix_Epoch_Time'], numeric_df[col], kind=kind, fill_value='extrapolate') 
                    for col in numeric_df.columns if col != 'Unix_Epoch_Time'}

    # Applying the interpolation function to the new time series
    resampled_data = {col: interp_funcs[col](new_time_series) for col in interp_funcs}

    # Creating the new DataFrame
    resampled_df = pd.DataFrame(resampled_data)
    resampled_df['Unix_Epoch_Time'] = new_time_series

    return resampled_df



def find_nearest(array, values):
    """
    Find the nearest value in 'array' for each value in 'values'.

    :param array: Sorted numpy array to search.
    :param values: Values for which to find the nearest in 'array'.
    :return: A list of indices representing the nearest value in 'array'.
    """
    nearest_indices = []
    array = np.asarray(array)

    for value in values:
        idx = np.searchsorted(array, value, side="left")

        if idx == 0:
            nearest_indices.append(idx)
        elif idx == len(array):
            nearest_indices.append(idx - 1)
        else:
            left_diff = np.abs(value - array[idx - 1])
            right_diff = np.abs(value - array[idx])

            if left_diff <= right_diff:
                nearest_indices.append(idx - 1)
            else:
                nearest_indices.append(idx)

    return nearest_indices




def create_50hz_dataframe(global_parsed_data):
    packet_types = ["IMU_0", "IMU_1", "RATE", "ATT", "XKF1_0", "XKF5_0", "XKF1_1", "RCOU", "BARO", "GPS"]

    min_time = min(global_parsed_data[ptype]['Unix_Epoch_Time'].min() for ptype in packet_types if ptype in global_parsed_data)
    max_time = max(global_parsed_data[ptype]['Unix_Epoch_Time'].max() for ptype in packet_types if ptype in global_parsed_data)

    # Floor the min_time to the nearest 20ms and generate the new time series
    floored_min_time = np.floor(min_time / 0.02) * 0.02
    # Generate the new time series and explicitly convert to a float type
    new_time_series = np.round(np.arange(floored_min_time, np.floor(max_time / 0.02) * 0.02, 0.02), 2)
    new_time_series = new_time_series.astype(np.float64)

    df_50hz = pd.DataFrame(new_time_series, columns=['Unix_Epoch_Time'])

    for ptype in packet_types:
        if ptype in global_parsed_data:
            print(f"Processing {ptype}...")

            input_df = global_parsed_data[ptype]

            # Remove 'mavpackettype' column if it exists
            if 'mavpackettype' in input_df.columns:
                input_df = input_df.drop(columns=['mavpackettype'])

            # Rename columns to avoid conflicts, except for 'Unix_Epoch_Time'
            input_df = input_df.rename(columns=lambda x: f"{ptype}_{x}" if x != 'Unix_Epoch_Time' else x)

            resampled_df = resample_to_50hz(input_df, df_50hz['Unix_Epoch_Time'], kind='linear')

            # Ensure no overlapping columns other than 'Unix_Epoch_Time'
            common_cols = df_50hz.columns.intersection(resampled_df.columns)
            common_cols = common_cols.drop('Unix_Epoch_Time')
            if not common_cols.empty:
                resampled_df = resampled_df.drop(columns=common_cols)

            # Merge with the main DataFrame
            df_50hz = df_50hz.merge(resampled_df, on='Unix_Epoch_Time', how='left')

            print(f"Finished processing {ptype}.")

    return df_50hz

def convert_bin_to_json(bin_file_path):
    global global_parsed_data
    # Clear the global_parsed_data before loading new data
    global_parsed_data = {}
    global allowed_export_packet_types
    allowed_packet_types = ["XKF1", "XKF5", "IMU", "GPS", "RCOU", "MODE"]
    mlog = mavutil.mavlink_connection(bin_file_path)
    parsed_data = defaultdict(list)
    fileInfo = {}

    imu_counter = 0
    xkf_counter = 0
    rcou_counter = 0
    xkf_skip = 4  # Skip count to maintain 5Hz for XKF (20Hz / 5Hz)
    imu_skip = 400  # Skip count to maintain 5Hz for IMU (400Hz / 1Hz)
    rcou_skip = 20  # Skip count to maintain 5Hz for RCOU (10Hz / 0.5Hz)

    packet_count = 0
    packet_type_counter = Counter()  # For detailed packet type counts
    batch_size = 100000  # Update the progress every 100000 packets
    last_emitted_progress = -5

    while True:
        msg = mlog.recv_msg()
        if msg is None:
            break
        msg_dict = msg.to_dict()
        packet_type = msg_dict.get("mavpackettype")
        packet_count += 1

        if packet_type in allowed_export_packet_types:
            key = determine_key_for_packet(msg_dict)
            if key not in global_parsed_data or not isinstance(global_parsed_data[key], list):
                global_parsed_data[key] = []  # Ensure it's a list
            global_parsed_data[key].append(msg_dict)

        if packet_type in allowed_packet_types:
            if packet_type == "IMU":
                if imu_counter % imu_skip == 0:
                    parsed_data[packet_type].append(msg_dict)
                imu_counter += 1
            elif packet_type == "XKF1" or packet_type == "XKF5":
                if "C" in msg_dict and msg_dict["C"] == 0:
                    if xkf_counter % xkf_skip == 0:
                        parsed_data[packet_type].append(msg_dict)
                    xkf_counter += 1
            elif packet_type == "RCOU":
                if rcou_counter % rcou_skip == 0:
                    parsed_data[packet_type].append(msg_dict)
                rcou_counter += 1
            elif packet_type == "MODE":
                mode_name = map_mode_number_to_name(msg_dict.get("Mode", -1))
                msg_dict["ModeName"] = mode_name
                parsed_data[packet_type].append(msg_dict)
            else:
                parsed_data[packet_type].append(msg_dict)

        if packet_count == 0:
            emit(
                "status",
                {
                    "message": f"Total MAVLink packets to scan: {mlog._count:,}",
                    "progress": 0,
                    "color": "#32CD32",
                },
                broadcast=True,
            )

        current_progress_percentage = mlog.percent

        # Emit progress in batches or every 5%
        if (
            packet_count % batch_size == 0
            or (current_progress_percentage - last_emitted_progress) >= 5
        ):
            last_emitted_progress = current_progress_percentage

            progress_percentage = "{:.2f}".format(current_progress_percentage)
            if packet_count >= 1000000:
                packet_count_formatted = "{:.2f}m".format(packet_count / 1000000)
            else:
                packet_count_formatted = "{:,}k".format(int(packet_count / 1000))

            emit(
                "status",
                {
                    "message": f"Imported {packet_count_formatted} packets... ({progress_percentage}%)",
                    "packet_types": dict(packet_type_counter),
                    "progress": current_progress_percentage * 0.9,
                    "color": "#1E90FF",
                },
                broadcast=True,
            )

    # Add closest GPS time to each DataFrame
    gps_data = global_parsed_data.get("GPS", [])
    if gps_data:
        gps_df = pd.DataFrame(gps_data)  # Convert the list to DataFrame
        gps_df["Unix_Epoch_Time"] = gps_df.apply(calculate_unix_epoch_time, axis=1)
        initial_unix_epoch_time = gps_df["Unix_Epoch_Time"].iloc[0]
        initial_time_us = gps_df["TimeUS"].iloc[0]

        for packet_type, data in global_parsed_data.items():
            if isinstance(data, list):  # Check if the data is a list
                df = pd.DataFrame(data)  # Convert list to DataFrame
            else:
                df = data  # If it's already a DataFrame, use it directly

            df["Unix_Epoch_Time"] = df["TimeUS"].apply(
                lambda x: calculate_unix_epoch_time_for_record(
                    x, initial_unix_epoch_time, initial_time_us
                )
            )
            global_parsed_data[packet_type] = df  # Update the dictionary with the modified DataFrame

    # Create the "50HZ" dataframe
    df_50hz = create_50hz_dataframe(global_parsed_data)

    # Append "50HZ" dataframe to global_parsed_data
    global_parsed_data["50HZ"] = df_50hz

    save_all_dataframes(global_parsed_data, bin_file_path)

    emit(
        "status",
        {
            "message": f"Total MAVLink packets imported: {packet_count:,}!",
            "progress": 90,
            "color": "#32CD32",
        },
        broadcast=True,
    )

    emit(
        "status",
        {"message": "Generating JSON data...", "color": "#32CD32"},
        broadcast=True,
    )

    # Process the GPS data first to obtain Unix_Epoch_Time
    gps_data = parsed_data.get("GPS", [])
    if gps_data:
        for entry in gps_data:
            entry["Unix_Epoch_Time"] = calculate_unix_epoch_time(entry)

        # Calculate initial values for time conversion
        initial_unix_epoch_time = gps_data[0]["Unix_Epoch_Time"]
        initial_time_us = gps_data[0]["TimeUS"]

        # Compute flight duration and Datetime_Chicago without seconds
        initial_unix_epoch_time = gps_data[0]["Unix_Epoch_Time"]
        final_unix_epoch_time = gps_data[-1]["Unix_Epoch_Time"]
        flight_duration = final_unix_epoch_time - initial_unix_epoch_time

        # Prepare file info
        file_name = os.path.basename(bin_file_path)
        chicago_tz = pytz.timezone("America/Chicago")
        initial_utc_time = dt.datetime.fromtimestamp(
            initial_unix_epoch_time, dt.timezone.utc
        )
        initial_datetime_chicago = initial_utc_time.astimezone(chicago_tz).replace(
            second=0, microsecond=0
        )

        fileInfo = {
            "file_name": file_name,
            "datetime_chicago": initial_datetime_chicago.isoformat(),
            "flight_duration_seconds": flight_duration,
        }

        # Convert Unix Epoch Time to Datetime in Chicago timezone
        chicago_tz = pytz.timezone("America/Chicago")
        for entry in gps_data:
            utc_time = dt.datetime.fromtimestamp(
                entry["Unix_Epoch_Time"], dt.timezone.utc
            )
            entry["Datetime_Chicago"] = utc_time.astimezone(chicago_tz).isoformat()

        # Now, update Unix_Epoch_Time for other data types
        for packet_type, data_list in parsed_data.items():
            if packet_type != "GPS":
                for entry in data_list:
                    entry["Unix_Epoch_Time"] = calculate_unix_epoch_time_from_timeus(
                        entry["TimeUS"], initial_unix_epoch_time, initial_time_us
                    )

    # Emit progress updates (adjust as needed)
    emit(
        "status",
        {"message": "Data conversion completed", "progress": 100, "color": "#32CD32"},
        broadcast=True,
    )

    # Convert the parsed data to JSON, maintaining separate folders for each packet type
    json_output = {packet_type: records for packet_type, records in parsed_data.items()}

    # Add fileInfo to JSON output
    json_output["file_info"] = fileInfo

    save_json(json_output, bin_file_path)
    
import math
import json

def save_json(data, bin_file_path):
    base_name = os.path.basename(bin_file_path)
    json_filename = f"{os.path.splitext(base_name)[0]}.json"
    file_path = os.path.join(UPLOAD_FOLDER, json_filename)

    # Function to recursively replace non-compliant float values
    def replace_invalid_value(item):
        if isinstance(item, float):
            if math.isnan(item) or math.isinf(item):
                return None  # Replace NaN or inf with None
        elif isinstance(item, dict):
            return {k: replace_invalid_value(v) for k, v in item.items()}
        elif isinstance(item, list):
            return [replace_invalid_value(elem) for elem in item]
        return item

    # Apply the replacement to the entire data
    processed_data = replace_invalid_value(data)

    with open(file_path, "w") as f:
        json.dump(processed_data, f)

    global current_json_file
    current_json_file = file_path


def calculate_unix_epoch_time(row):
    gps_week = row["GWk"]
    gps_milliseconds = row["GMS"]
    return (gps_week * 604800) + (gps_milliseconds / 1000) + 315964800


def convert_bin_to_export(
    bin_file_path, export_file_path, start_time_unix, end_time_unix, file_format="excel", file_types=[]
):
    start_time = time.time()

    print(f"bin_file_path: {bin_file_path}")
    print(f"export_file_path: {export_file_path}")
    print(f"start_time_unix: {start_time_unix}")
    print(f"end_time_unix: {end_time_unix}")

    mlog = mavutil.mavlink_connection(bin_file_path)

    def determine_key_for_packet(msg_dict):
        packet_type = msg_dict["mavpackettype"]
        instance = msg_dict.get("I", 0)

        if packet_type == "VIBE":
            instance = msg_dict.get("IMU", 0)
        elif packet_type == "MODE":
            mode_name = map_mode_number_to_name(msg_dict.get("Mode", -1))
            msg_dict["ModeName"] = mode_name
        elif "XKF" in packet_type:
            instance = msg_dict.get("C", 0)
            if instance not in [0, 1]:
                instance = 0  # Default to 0 if 'c' is not 0 or 1

        # Create a unique key for each packet type and instance
        if packet_type in ["IMU", "VIBE", "MAG"] or "XKF" in packet_type:
            key = f"{packet_type}_{instance}"
        else:
            key = packet_type

        return key

    # Initialize variables
    initial_unix_epoch_time = None
    initial_time_us = None

    global global_parsed_data

    # Clear the global_parsed_data before loading new data
    global_parsed_data = {}

    emit(
        "status",
        {"message": "Starting conversion...", "progress": 0, "color": "#FFD700"},
        broadcast=True,
    )

    progress_scale = 20 if file_format == "excel" else 80
    last_emitted_progress = -5  # Initialize to a value less than 0
    packet_count = 0
    packet_type_counter = Counter()  # For detailed packet type counts
    batch_size = 100000  # Update the progress every 100000 packets

    global_parsed_data, files_loaded = load_all_dataframes(bin_file_path)
    if not files_loaded:
    # Check if global_parsed_data has the required data and is not empty
    # if not global_parsed_data or all(len(df) == 0 for df in global_parsed_data.values()):
        print("DataFrames not in memory or they are empty. Loading...")

        # Parse the messages and build the data structure
        while True:
            msg = mlog.recv_msg()
            if msg is None:
                break

            msg_dict = msg.to_dict()
            packet_type = msg_dict.get("mavpackettype")
            packet_count += 1
            packet_type_counter[packet_type] += 1  # Count packet types

            if packet_type in allowed_export_packet_types:
                key = determine_key_for_packet(msg_dict)
                if key not in global_parsed_data or not isinstance(global_parsed_data[key], list):
                    global_parsed_data[key] = []  # Ensure it's a list
                global_parsed_data[key].append(msg_dict)

            # Calculate current progress
            current_progress_percentage = mlog.percent
            scaled_progress = current_progress_percentage * progress_scale / 100

            # Emit progress in batches or every 5%
            if (
                packet_count % batch_size == 0
                or (current_progress_percentage - last_emitted_progress) >= 5
            ):
                last_emitted_progress = (
                    current_progress_percentage  # Update last emitted progress
                )

                progress_percentage = "{:.2f}".format(current_progress_percentage)
                if packet_count >= 1000000:
                    packet_count_formatted = "{:.2f}m".format(packet_count / 1000000)
                else:
                    packet_count_formatted = "{:,}k".format(int(packet_count / 1000))

                emit(
                    "status",
                    {
                        "message": f"Imported {packet_count_formatted} packets... ({progress_percentage}%)",
                        "packet_types": dict(packet_type_counter),
                        "progress": 0,
                        "color": "#1E90FF",
                    },
                    broadcast=True,
                )

        save_all_dataframes(global_parsed_data, bin_file_path)

        emit(
            "status",
            {
                "message": f"MAVLink packets imported.",
                "progress": 0,
                "color": "#32CD32",
            },
            broadcast=True,
        )
    else:
        emit(
            "status",
            {
                "message": f"Loaded Mavlink packets from backend file.",
                "progress": 0,
                "color": "#32CD32",
            },
            broadcast=True,
        )

    # Now create DataFrames in one go
    print("File types:")
    print(file_types)
        
    if file_types is not None:  # Apply filtering only if file_types is explicitly provided
        data_frames = {key: pd.DataFrame(data_list).drop(columns=["mavpackettype"], errors="ignore") for key, data_list in global_parsed_data.items() if key in file_types}
        print("Available file types:")
        for key in data_frames.keys():
            print(key)
    else:
        # Include all data if file_types is None
        data_frames = {key: pd.DataFrame(data_list).drop(columns=["mavpackettype"], errors="ignore") for key, data_list in global_parsed_data.items()}
    
    # Add closest GPS time to each DataFrame
    # gps_df = data_frames.get("GPS", pd.DataFrame())
    # if not gps_df.empty:
    #     gps_df["Unix_Epoch_Time"] = gps_df.apply(calculate_unix_epoch_time, axis=1)
    #     initial_unix_epoch_time = gps_df["Unix_Epoch_Time"].iloc[0]
    #     initial_time_us = gps_df["TimeUS"].iloc[0]
    #     for packet_type, df in data_frames.items():
    #         df["Unix_Epoch_Time"] = df["TimeUS"].apply(
    #             lambda x: calculate_unix_epoch_time_for_record(
    #                 x, initial_unix_epoch_time, initial_time_us
    #             )
    #         )

    # Fetch Weather Data
    # gps_df = data_frames.get("GPS", pd.DataFrame())
    # min_time = gps_df["Unix_Epoch_Time"].min() - 900 if not gps_df.empty else None
    # max_time = gps_df["Unix_Epoch_Time"].max() + 900 if not gps_df.empty else None
    #

    # MICAH INSERT
    gps_df = data_frames.get("GPS", pd.DataFrame())

    # --- Safely handle missing time columns ---
    if not gps_df.empty and "Unix_Epoch_Time" in gps_df.columns:
        min_time = gps_df["Unix_Epoch_Time"].min() - 900
        max_time = gps_df["Unix_Epoch_Time"].max() + 900
    else:
        print("⚠️ GPS data missing 'Unix_Epoch_Time' — skipping weather fetch.")
        min_time, max_time = None, None
    # weather_data, _ = fetch_weather_data(min_time, max_time) if min_time and max_time else ([], False)

    # Debugging: Print each DataFrame, its columns, and row count
    for key, df in data_frames.items():
        print(f"DataFrame for {key}:")
        print(f"    Columns: {df.columns.tolist()}")
        print(f"    Number of Rows: {len(df)}")
        print("\n")

    # Fetching weather data and calculating average temperature
    weather_data, success = (
        fetch_weather_data(min_time, max_time) if min_time and max_time else ([], False)
    )
    avg_temperature_celsius = None

    if success and weather_data:
        avg_temperature_celsius = calculate_average_temperature(weather_data)
        emit(
            "status",
            {
                "message": f"Weather Data Retrieved! ({avg_temperature_celsius}C)",
                "color": "#9370DB",
            },
            broadcast=True,
        )
        if avg_temperature_celsius is not None:
            print(f"Average Temperature: {avg_temperature_celsius} Celsius")
        else:
            print("Failed to calculate average temperature")
    else:
        emit(
            "status",
            {"message": "Weather Data Unavailable", "color": "#FF0000"},
            broadcast=True,
        )
        print("Failed to fetch weather data or data is empty")

    # Normalize GPS Data to latitude and longitude meters if GPS DataFrame exists
    gps_df = data_frames.get("GPS")
    if gps_df is not None:
        gps_df = normalize_gps_data(gps_df)
        data_frames["GPS"] = gps_df  # Update the GPS DataFrame in the dictionary

    # Normalize barometric pressure to altitude meters if BARO DataFrame exists
    baro_df = data_frames.get("BARO")
    if baro_df is not None:
        # Use the average temperature in the altitude calculation
        baro_df["Altitude_Meters_Estimate"] = baro_df["Press"].apply(
            lambda x: convert_pressure_to_meters(x, avg_temperature_celsius)
        )

        # Normalize the altitude to start at zero
        if not baro_df["Altitude_Meters_Estimate"].empty:
            initial_altitude = baro_df["Altitude_Meters_Estimate"].iloc[0]
            baro_df["Altitude_Meters_Estimate"] -= initial_altitude

        data_frames["BARO"] = baro_df  # Update the BARO DataFrame in the dictionary

    # Assume 'data_frames' is your dictionary of DataFrames and 'bin_file_path' is the path of the bin file
    # save_dataframes(data_frames, bin_file_path)
    # Assume 'bin_file_path' is the path of the bin file
    # data_frames = load_dataframes(bin_file_path)

    # Debugging: Print each DataFrame, its columns, row count, and Unix_Epoch_Time range
    for key, df in data_frames.items():
        print(f"DataFrame for {key}:")
        print(f"    Columns: {df.columns.tolist()}")
        print(f"    Number of Rows: {len(df)}")
        
        # Check if 'Unix_Epoch_Time' column exists and print its start and end values
        if 'Unix_Epoch_Time' in df.columns:
            start_time = df['Unix_Epoch_Time'].iloc[0] if not df.empty else "DataFrame is empty"
            end_time = df['Unix_Epoch_Time'].iloc[-1] if not df.empty else "DataFrame is empty"
            print(f"    Start Unix Epoch Time: {start_time}")
            print(f"    End Unix Epoch Time: {end_time}")
        else:
            print("    Column 'Unix_Epoch_Time' not found in this DataFrame")

        print("\n")

    def unix_to_human_readable(unix_time):
        return datetime.datetime.utcfromtimestamp(unix_time).strftime('%Y-%m-%d %H:%M:%S')

    # Filter DataFrames based on Unix_Epoch_Time
    for packet_type, df in data_frames.items():
        # Print the start and end time in human-readable format
        print(f"Filtering {packet_type} DataFrame")
        print(f"Start time (Unix): {start_time_unix} ({unix_to_human_readable(start_time_unix)})")
        print(f"End time (Unix): {end_time_unix} ({unix_to_human_readable(end_time_unix)})")
        
        # Filter DataFrame rows where Unix_Epoch_Time is between start_time_unix and end_time_unix
        df_filtered = df[
            (df["Unix_Epoch_Time"] >= start_time_unix)
            & (df["Unix_Epoch_Time"] <= end_time_unix)
        ]
        
        # Print the start and end time of the DataFrame before filtering
        if not df.empty:
            print(f"DataFrame Start time: {unix_to_human_readable(df['Unix_Epoch_Time'].iloc[0])}")
            print(f"DataFrame End time: {unix_to_human_readable(df['Unix_Epoch_Time'].iloc[-1])}")
        else:
            print("DataFrame is empty.")

        # Print the number of packets in the resulting filtered dataframe
        print(f"Number of packets after filtering: {len(df_filtered)}\n")

        # Update the dataframe in the dictionary
        data_frames[packet_type] = df_filtered


    # Calculate the total number of rows across all DataFrames
    total_rows = sum(len(df) for df in data_frames.values())
    rows_written = 0

    # Determine file format and save accordingly
    if file_format == "excel":
        with pd.ExcelWriter(export_file_path, engine="xlsxwriter") as writer:
            for packet_type, df in data_frames.items():
                rows_written += len(df)

                # Calculate progress percentage
                progress_percentage = int((rows_written / total_rows) * 100) if total_rows > 0 else 0
                emit(
                    "status",
                    {
                        "message": f"Writing {packet_type} to Excel... ({progress_percentage}%)",
                        "progress": progress_percentage,
                        "color": "#20B2AA",
                    },
                    broadcast=True,
                )
                df.to_excel(writer, sheet_name=packet_type, index=False)

    elif file_format == "hdf5":
        with pd.HDFStore(export_file_path, mode="w") as hdf_store:
            for packet_type, df in data_frames.items():
                rows_written += len(df)

                progress_percentage = int((rows_written / total_rows) * 100) if total_rows > 0 else 0

                emit(
                    "status",
                    {
                        "message": f"Writing {packet_type} to HDF5... ({progress_percentage}%)",
                        "progress": progress_percentage,
                        "color": "#20B2AA",
                    },
                    broadcast=True,
                )
                hdf_store.put(packet_type, df, format="table", data_columns=True)
    elif file_format == "pkl":
        save_all_dataframes(data_frames, export_file_path, output_folder='./downloads')
        emit(
            "status",
            {
                "message": f"Writing done already! (pkl go brrr)",
                "progress": 100,
                "color": "#20B2AA",
            },
            broadcast=True,
        )
    else:
        raise ValueError("Unsupported file format")
    
    end_time = time.time()
    elapsed_time = end_time - start_time

    # Calculate hours, minutes, and seconds
    hours, rem = divmod(elapsed_time, 3600)
    minutes, seconds = divmod(rem, 60)

    # Build the time string based on the values of hours and minutes
    if hours > 0:
        time_str = "{:0>2}hr {:0>2}min {:05.2f}sec".format(int(hours), int(minutes), seconds)
    elif minutes > 0:
        time_str = "{:0>2}min {:05.2f}sec".format(int(minutes), seconds)
    else:
        time_str = "{:05.2f}sec".format(seconds)

    # Include the time string in the final message
    emit(
        "status",
        {
            "message": f"Data successfully saved to {file_format.upper()} file in {time_str}",
            "progress": 100,
            "color": "#32CD32",
        },
        broadcast=True,
    )



def save_df_to_json(df):
    global current_json_file
    # Generate a unique filename
    filename = f"{uuid.uuid4()}.json"
    file_path = os.path.join(UPLOAD_FOLDER, filename)

    # Convert DataFrame to JSON and save to the file
    with open(file_path, "w") as f:
        json.dump(df.to_dict(orient="records"), f, ignore_nan=True)

    current_json_file = file_path


# Function to load data from Excel
def load_data_from_excel(file_path):
    global df
    try:
        with pd.ExcelFile(file_path) as xls:
            df = pd.read_excel(
                file_path, sheet_name="ALL"
            )  # Assuming the sheet name is 'ALL'

            required_columns = [
                "GPS_Lat",
                "GPS_Lng",
                "BARO_Press",
                "IMU_0_AccX",
                "IMU_0_AccY",
                "IMU_0_AccZ",
                "RCOU_C1",
                "RCOU_C2",
                "RCOU_C3",
                "RCOU_C4",
                "RCOU_C8",
            ]

            if not all(column in df.columns for column in required_columns):
                return "Invalid ArduPilot-ish Data Structure!\nTalk to Jaedyn lol", 400

            df["Datetime_Chicago"] = (
                pd.to_datetime(df["Unix_Epoch_Time"], unit="s")
                .dt.tz_localize("UTC")
                .dt.tz_convert("America/Chicago")
            )
            df["Datetime_Chicago"] = df["Datetime_Chicago"].astype(str)

            save_df_to_json(df)

            return "File uploaded and data refreshed", 200
    except Exception as e:
        return str(e), 400


# Function to load data
def load_data(file_path):
    global df
    try:
        with open(file_path, "r") as file:
            df = pd.read_csv(file)

        # Specify the columns you expect
        required_columns = [
            "GPS_Lat",
            "GPS_Lng",
            "BARO_Press",
            "IMU_0_AccX",
            "IMU_0_AccY",
            "IMU_0_AccZ",
            "RCOU_C1",
            "RCOU_C2",
            "RCOU_C3",
            "RCOU_C4",
            "RCOU_C8",
        ]

        if not all(column in df.columns for column in required_columns):
            return "Invalid ArduPilot-ish CSV\nTalk to Jaedyn lol", 400

        # Existing code to convert Unix time etc...
        df["Datetime_Chicago"] = (
            pd.to_datetime(df["Unix_Epoch_Time"], unit="s")
            .dt.tz_localize("UTC")
            .dt.tz_convert("America/Chicago")
        )
        df["Datetime_Chicago"] = df["Datetime_Chicago"].astype(str)

        save_df_to_json(df)

        return "File uploaded and data refreshed", 200
    except Exception as e:
        return str(e), 400


def col_num_to_letter(n):
    """Convert a column number to an Excel-style column letter (e.g., 1 => 'A', 27 => 'AA')."""
    letter = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letter = chr(65 + remainder) + letter
    return letter


from flask_socketio import emit


@socketio.on("upload_and_convert")
def handle_upload_and_convert(json_data):
    global current_bin_file
    filename = json_data.get("filename")
    if not filename:
        emit("status", {"message": "Filename not provided."})
        return

    file_path = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(filename))
    # Assuming you've already saved the file and filename is valid
    # Perform your existing file conversion and data processing logic here.

    file_ext = os.path.splitext(filename)[1]

    if file_ext == ".bin":
        # Convert BIN to JSON for frontend UI
        current_bin_file = file_path
        convert_bin_to_json(file_path)
    else:
        emit("status", {"message": "Unsupported file type", "progress": 0})
        return

    emit(
        "status",
        {"message": "Bin File Upload Complete!", "progress": 0},
        broadcast=True,
    )


@app.route("/upload", methods=["POST"])
def file_upload():
    if "file" not in request.files:
        return jsonify(error="No file part"), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="No selected file"), 400

    if file:
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

        # Skip saving if file already exists, but continue processing
        if not os.path.exists(file_path):
            try:
                file.save(file_path)
            except IOError as e:
                # Log the exception for I/O errors
                print(f"An I/O error occurred: {e}")
                return jsonify(error="An error occurred while uploading the file"), 500
            except Exception as e:
                # Log general exceptions
                print(f"An error occurred: {e}")
                return jsonify(error="An error occurred while uploading the file"), 500

        return jsonify(message="File processed successfully"), 200

    return jsonify(error="Unknown error"), 500


def sanitize_filename(filename):
    # Strip off any file extension first
    filename_without_ext = re.sub(r"\..*$", "", filename)

    # Keep only word characters (alphanumeric & underscore)
    safe_str = re.sub(r"[^\w\s-]", "", filename_without_ext)

    return safe_str


@app.route("/is_data_available", methods=["GET"])
def is_data_available():
    return jsonify({"available": current_json_file is not None})


@app.route("/data", methods=["GET"])
def get_data():
    if current_json_file is None or not os.path.exists(current_json_file):
        return jsonify({"message": "No data available"}), 404

    return send_file(
        current_json_file,
        as_attachment=True,
        download_name="data.json",
        mimetype="application/json",
    )


@app.route("/speedtest", methods=["GET"])
def speedtest():
    # Generate a fixed size of data, e.g., 10 MB
    data_size_mb = 10
    data = "A" * data_size_mb * 1024 * 1024  # 1 MB = 1024 * 1024 bytes

    start_time = time.time()
    response = Response(data, mimetype="text/plain")
    end_time = time.time()

    transfer_time = end_time - start_time
    transfer_rate_mbps = (data_size_mb / transfer_time) * 8  # Convert MB/s to Mbps

    # Print transfer time and rate to the console
    print(f"Transfer Time: {transfer_time:.2f} seconds")
    print(f"Transfer Rate: {transfer_rate_mbps:.2f} Mbps")

    # Optionally, include these values in the response headers
    response.headers["X-Transfer-Time"] = str(transfer_time)
    response.headers["X-Transfer-Rate-Mbps"] = str(transfer_rate_mbps)

    return response


# Function to trim sheet data based on time range
def trim_sheet_data(
    sheet_df, start_time_unix, end_time_unix, time_column_name="Unix_Epoch_Time"
):
    return sheet_df[
        (sheet_df[time_column_name] >= start_time_unix)
        & (sheet_df[time_column_name] <= end_time_unix)
    ]


def get_mode_table(file_path):
    try:
        with pd.ExcelFile(file_path) as xls:
            if "MODE" not in xls.sheet_names:
                return None  # 'MODE' sheet does not exist
            mode_df = pd.read_excel(file_path, sheet_name="MODE")
            return mode_df
    except Exception as e:
        print(f"Error: {e}")
        return None  # Return None in case of any error


@app.route("/get_mode_table", methods=["GET"])
def fetch_mode_table():
    global current_excel_file  # Access the global variable
    if current_excel_file is None:
        return jsonify({"message": "No Excel file loaded"}), 400
    mode_df = get_mode_table(current_excel_file)
    if mode_df is None:
        return jsonify({"message": "MODE table not found or an error occurred"}), 404
    return jsonify(
        mode_df.to_dict(orient="records")
    )  # Return the MODE table data as JSON


@app.route("/data/csv", methods=["POST"])
def get_data_as_csv():
    if df.empty:
        return jsonify({"message": "No data available"}), 404

    data = request.json
    start_time = data["start_time"]
    end_time = data["end_time"]

    raw_filename = data.get("filename", "filtered_data.csv")
    filename = sanitize_filename(raw_filename)

    # Ensure the filename ends with .csv
    if not filename.endswith(".csv"):
        filename += ".csv"

    start_time_unix = pd.to_datetime(start_time).timestamp()
    end_time_unix = pd.to_datetime(end_time).timestamp()

    # Filter the DataFrame based on start and end times
    filtered_df = df[
        (df["Unix_Epoch_Time"] >= start_time_unix)
        & (df["Unix_Epoch_Time"] <= end_time_unix)
    ]

    if len(filtered_df) == 0:
        return jsonify({"message": "Filtered data is empty, no CSV generated"}), 404

    csv_str = filtered_df.to_csv(index=False)
    return Response(
        csv_str,
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename={filename}"},
    )


@socketio.on("export_data")
def handle_export_data(json_data):
    session_id = request.sid
    if not json_data:
        emit(
            "status", {"message": "No data provided", "progress": 0, "color": "#FF0000"}
        )
        return

    # Extract values from json_data
    start_time = json_data.get("start_time")
    end_time = json_data.get("end_time")
    raw_filename = json_data.get("filename", "exported_data.xlsx")
    file_format = json_data.get("format", "excel")  # Default to 'excel'
    file_types = json_data.get("file_types", None)
    print(f"File types in Export Data: {file_types}")

    if file_format == "excel":
        file_ext = ".xlsx"
    elif file_format == "hdf5":
        file_ext = ".h5"
    elif file_format == "pkl":
        file_ext = ".pkl"
    else:
        emit(
            "status",
            {"message": "Unsupported file format", "progress": 0, "color": "#FF0000"},
        )
        return

    # Ensure required data is provided
    if not start_time or not end_time:
        emit(
            "status",
            {
                "message": "Start time or end time not provided",
                "progress": 0,
                "color": "#FF0000",
            },
        )
        return

    filename = sanitize_filename(raw_filename)
    if not filename.endswith(file_ext):
        filename += file_ext

    # Convert start and end times from string to Unix timestamp
    start_time_unix = pd.to_datetime(start_time).timestamp()
    end_time_unix = pd.to_datetime(end_time).timestamp()

    # Path for the resulting Excel file
    export_file_path = os.path.join(DOWNLOAD_FOLDER, filename)

    if current_bin_file:
        convert_bin_to_export(
            current_bin_file,
            export_file_path,
            start_time_unix,
            end_time_unix,
            file_format,
            file_types=file_types,
        )
        # Check if the file was created successfully
        if os.path.exists(export_file_path):
            # Broadcast the file export completion message
            emit(
                "status",
                {
                    "message": "File Export Complete!",
                    "progress": 0,
                    "color": "#32CD32",
                },
                broadcast=True,
            )

            # Send the download URL only to the client who initiated the export
            download_url = f"/downloads/{filename}"  # Construct the download URL
            emit(
                "status",
                {"message": f"Download your file at {download_url}", "url": download_url},
                room=session_id,
            )
        else:
            emit(
                "status",
                {
                    "message": f"Failed to create {file_format.upper()} file",
                    "progress": 0,
                    "color": "#FF0000",
                },
            )
    else:
        emit(
            "status",
            {
                "message": "No binary file available to export",
                "progress": 0,
                "color": "#FF0000",
            },
        )


def list_files_in_directory(directory):
    """List files in a given directory with URLs and sizes, sorted by modification time (newest first), excluding JSON files."""
    files = []
    for filename in os.listdir(directory):
        if filename.lower().endswith(".json"):
            continue

        filepath = os.path.join(directory, filename)
        if os.path.isfile(filepath):
            file_stat = os.stat(filepath)

            # Convert mtime to datetime and localize to Chicago time zone
            mtime = datetime.datetime.fromtimestamp(
                file_stat.st_mtime, pytz.timezone("America/Chicago")
            )

            file_ext = os.path.splitext(filename)[1].lower()  # Convert extension to lowercase

            file_type = next(
                (
                    ext
                    for ext in ["bin", "xlsx", "h5", "pkl"]
                    if file_ext == f".{ext}"
                ),
                "other",
            )

            files.append(
                {
                    "filename": filename,
                    "url": f"/{directory}/{filename}",
                    "mtime": mtime,  # Unformatted modification time as datetime object
                    "formatted_mtime": mtime.strftime("%m/%d/%Y %I:%M:%S %p"),  # Formatted modification time
                    "size": file_stat.st_size,
                    "type": file_type
                }
            )

    return files


@app.route("/get_file_list")
def get_file_list():
    upload_files = list_files_in_directory(UPLOAD_FOLDER)
    download_files = list_files_in_directory(DOWNLOAD_FOLDER)

    # Combine both lists
    files = upload_files + download_files

    # Sort the combined list by unformatted 'mtime' in descending order
    files.sort(key=lambda x: x["mtime"], reverse=True)

    # Replace unformatted mtime with formatted mtime for display
    for file in files:
        file['mtime'] = file['formatted_mtime']
        del file['formatted_mtime']

    return jsonify(files)


@app.route("/load_json", methods=["POST"])
def load_json():
    if "filename" not in request.json:
        return jsonify(error="Filename not provided"), 400

    json_filename = request.json["filename"]
    bin_filename = json_filename.replace(".json", ".bin")
    json_file_path = os.path.join(app.config["UPLOAD_FOLDER"], json_filename)
    bin_file_path = os.path.join(app.config["UPLOAD_FOLDER"], bin_filename)

    if not os.path.exists(json_file_path):
        return jsonify(error="JSON file does not exist"), 404

    if not os.path.exists(bin_file_path):
        return jsonify(error="BIN file does not exist"), 404

    global current_json_file, current_bin_file
    current_json_file = json_file_path
    current_bin_file = bin_file_path
    global global_parsed_data
    global_parsed_data.clear()

    socketio.emit(
        "status",
        {"message": "File selection changed."},
        namespace='/'  # Specify the default namespace
    )
    
    return jsonify(message="JSON file loaded successfully"), 200


@app.route("/downloads/<filename>")
def download_file(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)


@app.route("/uploads/<filename>")
def upload_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        "static", "favicon.ico", mimetype="image/vnd.microsoft.icon"
    )


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
