# hk-roads

Hong Kong's road network (Transport Department, Road Network 2nd Generation): every layer of the file geodatabase, baked weekly
by GitHub Actions into GeoParquet (one file per layer) and one PMTiles file, published to Hugging Face
([dataset](https://huggingface.co/datasets/alreadyopen/hk-roads), [bucket](https://huggingface.co/buckets/alreadyopen/hk-roads)) and
mirrored to [Kaggle](https://www.kaggle.com/datasets/helenkwok/hk-roads). Made from
[pipeline-template](https://github.com/AlreadyOpen/pipeline-template): see it for the publish gate, checks and lessons.

Run locally (Python 3.13, tippecanoe 2.79.0): `pip install -r requirements.txt && python bake.py`.

Code: Apache-2.0. Data: Transport Department, under the [DATA.GOV.HK Terms and Conditions](https://data.gov.hk/en/terms-and-conditions).
