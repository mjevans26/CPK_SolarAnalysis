# -*- coding: utf-8 -*-
"""PostprocessSolar.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1D8xPgTrDyl_ZCaomxV5ae7oz-aF0FY7J

## Setup

### Install packages
"""
import geopandas as gpd
import pandas as pd
import rasterio as rio
import ee

from shapely.geometry import Point, Polygon
import numpy as np
from numpy.random import normal
import json

"""### Authenticate"""

auth.authenticate_user()

drive.mount('/content/drive')

ee.Authenticate()
ee.Initialize()

"""## Clean and Harmonize solar arrays

### Create GeoDataFrame with Annual Polygons
In this step we clean and improve the original data produced from our AI output. Assuming any arrays detected at time t should be present at time t+1, we iteratively dissolve overlaping polygons in each year, and then dissolve the resulting polygons with overlapping polygons in the following year. This fills in gaps to ensure that polygon Sij in year j = n at location i contains all polygons Sij for j<n. The resulting GeoDataFrame includes rows for each solar array in each year that it was present (array x year).
"""

def eliminate_overlap(gdf, group):
  """Eliminate overlapping polygons within a group
  Args:
    gdf (GeoDataFrame):
    group (str): column name contianing group labels
  Return:
    GeoDataFrame:
  """
  dissolved = gdf.dissolve(group, as_index = False)
  exploded = dissolved.explode('geometry', True)
  return(exploded)

def harmonize_years(gdf, group):
  """Iterate over a geodataframe by group eliminating overlapping polygons

  This funciton is analagous to a groupby and reduction with groups acting as elements
  provided to the reducer. Iterate over levels of a grouping variable, dissolving polygons 
  in the next group with output from the previous iteration.
  Args:
    gdf (GeoDataFrame): polygons over which to iterate. must contain a grouping variable
    group (str): column name of grouping variable.
  Returns: 
    GeoDataFrame
  """ 
  groups = gdf[group].unique()
  groups.sort()
  print(groups)
  gdflist = []
  for i, g in enumerate(groups):
    current = gdf[gdf[group] == g]
    if i == 0:
      eliminated = eliminate_overlap(current, group)
      gdflist.append(eliminated)
    else:
      combined = current.append(gdflist[-1])
      dissolved = combined.dissolve()
      exploded = dissolved.explode('geometry', True)
      exploded['system:time_start'] = g
      gdflist.append(exploded)
  gdf_final = gpd.GeoDataFrame(pd.concat(gdflist, ignore_index = True), crs = gdflist[0].crs)
  return gdf_final

# read our per-state data from GEE into GeoDataFrames
fs = ['NY_solar', 'DE_solar', 'PA_solar', 'VA_solar', 'MD_solar']
files = [f'/content/drive/MyDrive/{f}.geojson' for f in fs]
gdfs = [gpd.read_file(f, driver = 'GeoJON') for f in files]
gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index = True), crs = gdfs[0].crs)

len(gdf)

annualGDF = harmonize_years(gdf, 'system:time_start')

annualGDF.head()
# print(len(final))

# check that the number of arrays each year make sense (i.e should be increasing)
annualGDF.groupby('system:time_start').size()

# write data to GCS for ingestion to GEE
outFile = 'CPK_solarJun21_annual'
dstFile = 'gs://cic-solar-mevans'
annualGDF.to_file(outFile+'.shp')

!gsutil mv {outFile}* {dstFile}

!earthengine upload table --asset_id projects/mevans-cic-solar/assets/Sampling/CPK_solarJun21_annual {dstFile+'/'+outFile+'.shp'}

del gdf

"""### Create Dataframe with unique polygons per year
For statistical analysis, a GeoDataFrame with the same solar array
 represented in multiple years leads to pseudoreplication. 
 Therefore, we need an alternatively structure dataset in which each row
 represents a solar array polygon in the first year that it appeared.
"""

# get the year of all overlapping polygons
joined = gpd.sjoin(annualGDF, annualGDF[['geometry', 'year']],'left','intersects')
# aggreate overlapping polygons retaining minimum year (i.e. year of appearance)
dissolved = joined.dissolve(by = joined.index, aggfunc = 'min')
# aggregate polygons per year of appearance
dissolved2 = dissolved.dissolve(by = 'year_right', aggfunc = 'first', as_index = False)
# convert multipart to single polygon geometries
exploded = dissolved2.explode(ignore_index = True, index_parts = False)

outFile = 'CPK_solarJun21_firstyear'
dstFile = 'gs://cic-solar-mevans'
exploded.to_file(outFile+'.shp')

!gsutil mv {outFile}* {dstFile}

!earthengine upload table --asset_id projects/mevans-cic-solar/assets/Sampling/CPK_solarJun21_firstyear {dstFile+'/'+outFile+'.shp'}

"""## Sampling"""

STATES = ee.FeatureCollection("TIGER/2018/States")
states = ['Pennsylvania', 'Virginia', 'New York', 'Delaware', 'Maryland', 'District of Columbia']
studyArea = STATES.filter(ee.Filter.inList('NAME', states))
TRACTS = ee.FeatureCollection("TIGER/2010/Tracts_DP1")
tracts = TRACTS.filterMetadata('aland10', 'greater_than', 10)
annualArrays = ee.FeatureCollection('projects/mevans-cic-solar/assets/Sampling/CPK_solarJun21_annual')
firstArrays = ee.FeatureCollection('projects/mevans-cic-solar/assets/Sampling/CPK_solarJun21_firstyear')

def add_data(ft):
  return(ft.set({
      'year': ee.Number.parse(ee.String(ft.get('system_tim')).slice(0,4)),
      'area': ft.area(),
      'state': ee.Feature(STATES.filterBounds(ft.geometry()).first()).get('NAME')
    })
  )

"""### Random polygons
We create random polygons to represent areas that could have been, but were not, developed for solar energy. Random polygons are 2:1 rectangles with size drawn from a normal distribution based on the observed mean and variance in mapped solar array sizes.
"""

# we will generate random 
def calc_area_sides(ft):
  """Estimates the side of a GEE Feature based on its area, assuming a 2:1 rectangle
  Params:
   ft (ee.Feature)
  Returns:
    ee.Feature: input features with additional attributes 'area' and 'side'
  """
  area = ft.area()
  side = area.divide(2).sqrt().divide(2)
  return ft.set('area', area, 'side', side)

solar_arrays = firstArrays.map(calc_area_sides)

# we want random polygons in proportion to real solar arrrays per state
# DEPRECATED: This is not true! We will include a random intercept per state, so we want information about baseline differences in probability of arrays in each
def generate_random_perstate(state):
  """Generates 5x random points for the solar arrays within each state
  Args:
    state (str): TIGER name of state
  Returns:
    ee.FeatureCollection: features representing random points
  """
  st = studyArea.filterMetadata('NAME', 'equals', state)
  aoi = tracts.filterBounds(st)
  array_subset = solar_arrays.filterBounds(st).filterMetadata('system_tim', 'equals', '2021-06-01')
  narrays = array_subset.size()
  random = ee.FeatureCollection.randomPoints(aoi, narrays.multiply(5))
  return random

# we will generate 5x the number of arrays present in 2021
n = annualArrays.filterMetadata('system_tim', 'equals', '2021-06-01').size().getInfo()
print(n)
randFtCol = ee.FeatureCollection.randomPoints(studyArea, n*5)

# Get the JSON objects for random points so we can build random rectangels using GeoPandas
crs = randFtCol.geometry().projection().crs().getInfo()
randomPts = randFtCol.geometry().getInfo()

len(randomPts['coordinates'])

shapelyPts = [Point(coords) for coords in randomPts['coordinates']]

randomGdf = gpd.GeoDataFrame(geometry = shapelyPts, crs = crs)

# Alternatively, export our random points 
task = ee.batch.Export.table.toDrive(collection=randFtCol, description='randompoints', fileFormat = 'GeoJSON')
task.start()

randomGdf = gpd.read_file('/content/drive/MyDrive/randompoints.geojson', driver = 'GeoJSON')

randomGdf.head()

def make_random_rectangles(gdf, loc, scale):
  """Construct 2y x y and y x 2y rectangles at a location with y randomly determined by mean and std
  Args:
    gdf (GeoDataFrame): Points at which rectangles will be created
    loc (float): mean size of short side of rectangle
    scale (float): std of size of short side of rectangle
  Returns: 
    GeoDataFrame: randomly sized rectangles at input points
  """
  # create an array of random radii based on input mean and std
  geom = gdf.geometry.to_crs('epsg:3857')
  x = geom.x
  y= geom.y
  random_radii = normal(loc = loc, scale = scale, size = len(gdf))
  # this construct alternately creates 2y x y and y x 2y rectangles
  rectangles = [Polygon([[x[i]+(i%2 +1)*random_radii[i], y[i]+(2-i%2)*random_radii[i]], [x[i]+(i%2 +1)*random_radii[i],  y[i]-(2-i%2)*random_radii[i]], [x[i]-(i%2 +1)*random_radii[i], y[i]-(2-i%2)*random_radii[i]], [x[i]-(i%2 +1)*random_radii[i], y[i]+(2-i%2)*random_radii[i]], [x[i]+(i%2 +1)*random_radii[i], y[i]+(2-i%2)*random_radii[i]]]) for i in range(len(random_radii))]
  return gpd.GeoDataFrame(geometry = rectangles, crs = 'epsg:3857')

# we may want to keep random polygons in GEE, so define an equivalent method
def make_random_circles_ee(ftCol, loc, scale):
  """Construct a rectangle at a centroid with random width and height within a specified range
  Args:
    gdf:
    loc:
    scale:
  Return:
  """
  # create an array of random radii based on input mean and std
  def random_radii(ft):
    radii = normal(loc = loc, scale = scale, size = 1)[0]
    return ft.buffer(radii)

  buffered = ftCol.map(random_radii)
  return buffered

# calculate the mean and std of the short side of rectangles with area equal to mapped solar arrays
side_mean = solar_arrays.aggregate_mean('side').getInfo()
side_sd = solar_arrays.aggregate_sample_sd('side').getInfo()
print('mean side:', side_mean, 'side_sd:', side_sd)

# generate random rectangles at our random points
rectanglesGdf = make_random_rectangles(randomGdf, side_mean, side_sd)
len(rectanglesGdf) == len(randomGdf)

# convert rectangles to GEE FeatureCollection and assign year = 2030 to distinguish from real arrays
rectanglesJson = json.loads(rectanglesGdf.geometry.to_crs('epsg:4326').to_json())
rectanglesFtCol = ee.FeatureCollection(rectanglesJson).map(lambda x:x.set('system_tim', '2030-06-01'))

collection = rectanglesFtCol.map(add_data)
task = ee.batch.Export.table.toAsset(collection = collection, description = 'random rectangles', assetId = 'projects/mevans-cic-solar/assets/Sampling/random_arrays' )
task.start()

"""### Sample raster data
We use GEE to efficiently sample covariates at real and random solar arrays that are represented as rasters
"""

SRTM = ee.Image("USGS/SRTMGL1_003")
STATES = ee.FeatureCollection("TIGER/2018/States")
states = ['Pennsylvania', 'Virginia', 'New York', 'Delaware', 'Maryland', 'District of Columbia']
studyArea = STATES.filter(ee.Filter.inList('NAME', states))
DEM = ee.Image("USGS/3DEP/10m")
CDL16 = ee.Image("USDA/NASS/CDL/2016").select('cultivated').clip(studyArea)
CDL17 = ee.Image("USDA/NASS/CDL/2017").select('cultivated').clip(studyArea)
CDL18 = ee.Image("USDA/NASS/CDL/2018").select('cultivated').clip(studyArea)
CDL19 = ee.Image("USDA/NASS/CDL/2019").select('cultivated').clip(studyArea)
CDL20 = ee.Image("USDA/NASS/CDL/2020").select('cultivated').clip(studyArea)
randomArrays = ee.FeatureCollection("projects/mevans-cic-solar/assets/Sampling/random_arrays")
arrays = ee.FeatureCollection("projects/mevans-cic-solar/assets/Sampling/CPK_solarJun21_firstyear_0b30d1b5ad9a0b32ce7b134ff05774e8")

# create predictor variables based on NLCD 2016 data
nlcd16 = ee.Image('USGS/NLCD_RELEASES/2019_REL/NLCD/2016').clip(studyArea)
nlcd19 = ee.Image('USGS/NLCD_RELEASES/2019_REL/NLCD/2019').clip(studyArea)

landcover16 = nlcd16.select('landcover')
landcover19 = nlcd19.select('landcover')

# percent impervious surface per pixel
impervious16 = nlcd16.select('impervious').rename('impervious16')
impervious19 = nlcd19.select('impervious')

# 0-1 indicator of non-agricultural 'open' land (e.g. fields, lawns, etc.)
open16 = landcover16.eq(52).Or(landcover16.eq(71)).Or(landcover16.eq(21)).Or(landcover16.eq(22)).rename('open16')
open19 = landcover19.eq(52).Or(landcover19.eq(71)).Or(landcover19.eq(21)).Or(landcover19.eq(22)).rename('open')

# percent tree cover per pixel
tree_cover16 = landcover16.eq(41).Or(landcover16.eq(42)).Or(landcover16.eq(43)).Or(landcover16.eq(90)).rename('tree_cover16')
tree_cover19 = landcover19.eq(41).Or(landcover19.eq(42)).Or(landcover19.eq(43)).Or(landcover19.eq(90)).rename('tree_cover')

# CDL natively comes 1 = not cultivated, 2 = cultivated
# create a 0-1 indicator of agriculture per pixel
cdl16 = CDL16.subtract(1).rename('cultivaed16')
cdl17 = CDL17.subtract(1)
cdl18 = CDL18.subtract(1)
cdl19 = CDL19.subtract(1)
cdl20 = CDL20.subtract(1)

# 10 m slope
dem = DEM.clip(studyArea)
slope = ee.Terrain.slope(dem).rename('slope')

# we need to split arrays by year and add variables accordingly
years = [2017, 2018, 2019, 2020, 2021]

dataDict = {
    2017: slope.addBands(cdl16).addBands(tree_cover16).addBands(impervious16).addBands(open16),
    2018: slope.addBands(cdl17).addBands(tree_cover16).addBands(impervious16).addBands(open16).addBands(cdl16),
    2019: slope.addBands(cdl18).addBands(tree_cover16).addBands(impervious16).addBands(open16).addBands(cdl16),
    2020: slope.addBands(cdl19).addBands(tree_cover19).addBands(impervious19).addBands(open19).addBands(cdl16).addBands(tree_cover16).addBands(impervious16).addBands(open16),
    2021: slope.addBands(cdl20).addBands(tree_cover19).addBands(impervious19).addBands(open19).addBands(cdl16).addBands(tree_cover16).addBands(impervious16).addBands(open16)
}

# define a funciton we can map over our list of years to sample appropriate data
def add_covariates_by_year(year):
  subset = arrays.filter(ee.Filter.eq('year', year))
  data = dataDict[year]
  sample = data.reduceRegions(
      collection = subset,
      reducer = ee.Reducer.mean(),
      scale = 10,
      tileScale = 12
  )
  return(sample)

ftColList = [add_covariates_by_year(year) for year in years]

arraySample = ee.FeatureCollection(ftColList).flatten()

arraySample.size().getInfo()

# save raster predictor data 
task1 = ee.batch.Export.table.toAsset(
  collection= arraySample,
  assetId = 'projects/mevans-cic-solar/assets/Outputs/CPK_arrays_GEEcovariates',
  description= 'CPK_arrays_GEEcovariates'
)

task1.start()

# and export to Drive
task2 = ee.batch.Export.table.toDrive(
    collection = arraySample,
    description = 'CPK_arrays_GEEcovariates',
    fileFormat = 'GeoJSON'
)

task2.start()

# now we sample raster covariates at random rectangles
data = dataDict[2021]
randomSample = data.reduceRegions(
      collection = randomArrays,
      reducer = ee.Reducer.mean(),
      scale = 10,
      tileScale = 12
  )

# save raster predictor data 
task3 = ee.batch.Export.table.toAsset(
  collection= randomSample,
  assetId = 'projects/mevans-cic-solar/assets/Outputs/CPK_random_GEEcovariates',
  description= 'CPK_random_GEEcovariates'
)

task3.start()

# and export to Drive
task4 = ee.batch.Export.table.toDrive(
    collection = randomSample,
    description = 'CPK_random_GEEcovariates',
    fileFormat = 'GeoJSON'
)

task4.start()