import tempfile
from typing import Optional , Optional, Union, Any, Dict, List
import numpy as np
from sentinelhub import DataCollection, MimeType, SentinelHubRequest
import boto3
import rasterio
import numpy as np
from datetime import datetime
from pathlib import Path


def get_true_color_images(aoi_bbox, aoi_size, config, time_interval):
    evalscript_true_color = """
        //VERSION=3

        function setup() {
            return {
                input: [{
                    bands: ["B02", "B03", "B04"]
                }],
                output: {
                    bands: 3
                }
            };
        }

        function evaluatePixel(sample) {
            return [sample.B04, sample.B03, sample.B02];
        }
    """

    request_true_color = SentinelHubRequest(
        evalscript=evalscript_true_color,
        input_data=[
            SentinelHubRequest.input_data(
                data_collection=DataCollection.SENTINEL2_L2A.define_from(
                    name="s2l2a", service_url="https://sh.dataspace.copernicus.eu"
                ),
                time_interval=time_interval,
                other_args={"dataFilter": {"mosaickingOrder": "leastCC"}},
            )
        ],
        responses=[SentinelHubRequest.output_response("default", MimeType.PNG)],
        bbox=aoi_bbox,
        size=aoi_size,
        config=config,
    )

    data = request_true_color.get_data()
    if isinstance(data, list) and len(data) > 0:
        # Convert to numpy array if not already
        return np.array(data[0])
    else:
        return None


# ...existing code...

def get_sar_image(aoi_bbox, aoi_size, config, time_interval, polarization="VV"):
    """
    Download a Sentinel-1 SAR image (single polarization) as a numpy array.

    Args:
        aoi_bbox: BBox object defining the area of interest.
        aoi_size: Tuple (width, height) in pixels.
        config: SHConfig object.
        time_interval: Tuple (start_date, end_date) as strings.
        polarization: "VV" or "VH" (default: "VV").

    Returns:
        np.ndarray or None: SAR image as a NumPy array (height, width), dtype is typically float32, 
        and values are scaled reflectance or backscatter coefficients as provided by SentinelHub (not scaled to 0-255).
    """
    evalscript_sar = f"""
    //VERSION=3
    function setup() {{
        return {{
            input: [{{
                bands: ["{polarization}"]
            }}],
            output: {{
                bands: 1
            }}
        }};
    }}

    function evaluatePixel(sample) {{
        return [sample.{polarization}];
    }}
    """

    request_sar = SentinelHubRequest(
        evalscript=evalscript_sar,
        input_data=[
            SentinelHubRequest.input_data(
                data_collection=DataCollection.SENTINEL1_IW,
                time_interval=time_interval,
                other_args={"dataFilter": {"polarization": polarization, "acquisitionMode": "IW"}}
            )
        ],
        responses=[SentinelHubRequest.output_response("default", MimeType.TIFF)],
        bbox=aoi_bbox,
        size=aoi_size,
        config=config,
    )

    data = request_sar.get_data()
    if isinstance(data, list) and len(data) > 0:
        return np.array(data[0])
    else:
        return None
    
    from sentinelhub import DataCollection, MimeType, SentinelHubRequest
def list_sar_images_sentinelhub(aoi_bbox, time_interval, config, polarization="VV"):
    """
    List available Sentinel-1 SAR images from SentinelHub Catalog for a given area and time interval.

    Args:
        aoi_bbox: BBox object defining the area of interest.
        time_interval: Tuple (start_date, end_date) as strings.
        config: SHConfig object.
        polarization: "VV" or "VH" (default: "VV").

    Returns:
        List[dict]: List of dictionaries with all attributes for each SAR image.
    """

    catalog = SentinelHubCatalog(config=config)
    search_iterator = catalog.search(
        DataCollection.SENTINEL1_IW,
        bbox=aoi_bbox,
        time=time_interval,
        query={
            "polarization": {"eq": polarization},
            "acquisitionMode": {"eq": "IW"}
        }
    )
    results = []
    for item in search_iterator:
        results.append(item['properties'])
    return results
from sentinelhub import BBox, CRS, bbox_to_dimensions, SentinelHubCatalog, SHConfig

# ...existing code...


def download_true_color_from_s3(s3_url, aws_access_key_id=None, aws_secret_access_key=None, region_name="eu-central-1"):
    """
    Download a true color image from a Sentinel S3 bucket and return as a numpy array.

    Args:
        s3_url (str): S3 URL to the true color image (e.g., 's3://sentinel-s2-l2a/...')
        aws_access_key_id (str, optional): AWS access key ID.
        aws_secret_access_key (str, optional): AWS secret access key.
        region_name (str): AWS region.

    Returns:
        np.ndarray: True color image as numpy array (height, width, bands).
    """
    assert s3_url.startswith("s3://")
    s3_path = s3_url[5:]
    bucket, key = s3_path.split("/", 1)

    session = boto3.Session(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region_name
    )
    s3 = session.client('s3')

    with tempfile.NamedTemporaryFile(suffix=".tiff") as tmpfile:
        s3.download_file(bucket, key, tmpfile.name)
        with rasterio.open(tmpfile.name) as src:
            data = src.read()  # (bands, height, width)
            data = np.transpose(data, (1, 2, 0))  # (height, width, bands)
    return data

# Example usage:
# true_color_img = download_true_color_from_s3("s3://sentinel-

def download_sar_from_s3(s3_url, aws_access_key_id=None, aws_secret_access_key=None, region_name="eu-central-1"):
    """
    Download a SAR image from a Sentinel S3 bucket and return as a numpy array.

    Args:
        s3_url (str): S3 URL to the SAR image (e.g., 's3://sentinel-s1-l1c/...')
        aws_access_key_id (str, optional): AWS access key ID.
        aws_secret_access_key (str, optional): AWS secret access key.
        region_name (str): AWS region.

    Returns:
        np.ndarray: SAR image as numpy array.
    """
    # Parse bucket and key from s3_url
    assert s3_url.startswith("s3://")
    s3_path = s3_url[5:]
    bucket, key = s3_path.split("/", 1)

    session = boto3.Session(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region_name
    )
    s3 = session.client('s3')

    # Download file to a temporary location
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".tiff") as tmpfile:
        s3.download_file(bucket, key, tmpfile.name)
        with rasterio.open(tmpfile.name) as src:
            sar_array = src.read(1)  # Read the first band
    return sar_array

# Example usage:
# sar_array = download_sar_from_s3("s3://sentinel-s1-l1c/GRD/2022/7/1/IW/DV/S1A_IW_GRDH_1SDV_20220701TXXXXXX_XXXXXX_XXXXXX_XXXX.SAFE/measurement/s1a-iw-grd-vv-20220701tXXXXXX-XXXXXX-XXXXXX-XXXX.tiff")

import rasterio
import numpy as np
from sentinelhub import SentinelHubCatalog

def extract_raster_data(file_path, band=1):
    """
    Extract raster data from a SAR image file.

    Args:
        file_path (str): Path to the SAR image file (e.g., .tif or .tiff).
        band (int): Band number to read (default: 1).

    Returns:
        np.ndarray: Raster data as a NumPy array.
    """
    with rasterio.open(file_path) as src:
        data = src.read(band)
    return data

# Example usage:
# sar_data = extract_raster_data("sar_image.tif")



def extract_true_color_raster(file_path):
    """
    Extract raster data from a true color image file (multi-band).

    Args:
        file_path (str): Path to the true color image file (e.g., .tif, .tiff, .png).

    Returns:
        np.ndarray: Raster data as a NumPy array (bands, height, width) or (height, width, bands).
    """
    with rasterio.open(file_path) as src:
        data = src.read()  # shape: (bands, height, width)
        # Optionally, transpose to (height, width, bands) for visualization
        data = np.transpose(data, (1, 2, 0))
    return data

# Example usage:
# true_color_data = extract_true_color_raster("true_color_image.tif")

def time_interval_to_string(time_interval):
    """
    Return a human-readable string for a time interval tuple.
    """
    return f"{time_interval[0]}__{time_interval[1]}"


def slots_to_query(start_date, end_date, n_chunks):
    start = datetime(start_date.year, start_date.month, start_date.day)
    end = datetime(end_date.year, end_date.month, end_date.day)
    tdelta = (end - start) / n_chunks
    edges = [(start + i * tdelta).date().isoformat() for i in range(n_chunks)]
    slots = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    return slots

def getBBox(feature_name, region_name):
    if feature_name == "Nizhnesvirsky_lower_bay":
        # Coordinates: [west, south, east, north]
        nizhnesvirsky_bbox_coords = [33.66, 60.76, 33.70, 60.80]  # [min_lon, min_lat, max_lon, max_lat]
        from sentinelhub import BBox, CRS
        if region_name == "Europe":
            # Use UTM zone 36N for this region for better true color image reprojection
            return BBox(bbox=nizhnesvirsky_bbox_coords, crs=CRS.UTM_36N)
        else:
            # Default to WGS84
            return BBox(bbox=nizhnesvirsky_bbox_coords, crs=CRS.WGS84)


def _locate_geojson(filename: str = "Nizhnesvirsky_lower_bay.geojson") -> Optional[str]:
    cwd = Path.cwd()
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    candidates = [
        cwd / filename,
        script_dir / filename,
        repo_root / filename,
        repo_root / "true_color_images" / filename,
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None