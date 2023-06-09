import argparse
import os
import logging
import json
import time
import numpy
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score
)
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder
import pickle
import shutil

import os
os.environ["AZURE_ML_CLI_PRIVATE_FEATURES_ENABLED"] = "True"

from azure.identity import ManagedIdentityCredential
from azureml.featurestore import FeatureStoreClient
from azureml.featurestore import init_online_lookup
from azureml.featurestore import get_online_features


print("here")

def init():
    """
    This function is called when the container is initialized/started, typically after create/update of the deployment.
    You can write the logic here to perform init operations like caching the model in memory
    """

    global model
    
    # load the model
    print("check model path")

    model_path = os.path.join(
        os.getenv("AZUREML_MODEL_DIR"), "model_output/clf.pkl"
    )



    with open(model_path, 'rb') as pickle_file:
        model = pickle.load(pickle_file)
    # AZUREML_MODEL_DIR is an environment variable created during deployment.
    # It is the path to the model folder (./azureml-models/$MODEL_NAME/$VERSION)
    # Please provide your model's folder name if there is one
    
        
    # load feature retrieval spec
    print("load feature spec")


    credential = ManagedIdentityCredential()

    spec_path = os.path.join(os.getenv("AZUREML_MODEL_DIR"), "model_output")

    global features

    featurestore = FeatureStoreClient(
        credential = credential
    )

    features = featurestore.resolve_feature_retrieval_spec(spec_path)


    init_online_lookup(features, credential)

    time.sleep(20)

    logging.info("Init complete")


def run(raw_data):

    logging.info("model 1: request received")
    logging.info(raw_data)
    print(raw_data)

    data = json.loads(raw_data)["data"]

    obs = pd.DataFrame(data, index=[0])
    df=get_online_features(features, obs)
    print("feature retrieved")
    print(df)

    logging.info("model 1: feature joined")
    
    #data = numpy.array(data)
    data = df.drop(["accountID"], axis="columns").to_numpy()
    result = model.predict(data)
    logging.info("Request processed")
    return result.tolist()

