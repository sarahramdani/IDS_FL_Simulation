import os
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

import functools
import math
from collections import OrderedDict
from typing import cast, Callable
import time

import flwr
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import tensorflow as tf
from flwr.common import Metrics
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import confusion_matrix
from numpy.typing import ArrayLike, NDArray
from flwr.simulation.ray_transport.utils import enable_tf_gpu_growth
from tensorflow import keras
from flwr.server.history import History
from flwr.common import ndarrays_to_parameters, parameters_to_ndarrays
from flwr.server.strategy import FedAvg
from flwr.server.client_proxy import ClientProxy
from flwr.common import (
    EvaluateRes,
    FitRes,
)
from flwr.common.typing import Scalar, Union, Optional


# print("TensorFlow version:", tf.__version__)
# print(f"Num GPUs Available: {len(tf.config.list_physical_devices('GPU'))}")
# enable_tf_gpu_growth()

def get_data(file_name):
    try:
        print("Loaded training data from " + str(file_name))
        # load the data from the npz file
        data = np.load(file_name)
        print(data)
        X_train = data["x_train"]
        X_test = data["x_test"]
        y_train = data["y_train"]
        y_test = data["y_test"]
    except Exception:
        raise IOError("Unable to load training data from path " "provided in config file: " + file_name)
    print("Client : " + str(file_name) + " has " + str(len(X_train)) + " training points and " + str(len(X_test)) + " testing points")
    print("Client : " + str(file_name) + " has " + str(len(y_train)) + " training labels and " + str(len(y_test)) + " testing labels")
    return X_train, y_train, X_test, y_test


BATCH_SIZE = 128
NUM_EPOCHS = 10
NUM_ROUNDS = 10


NUM_CLIENTS = 3
partitions = []

for n in range(NUM_CLIENTS):
    data_temp = get_data("data_party" + str(n) + ".npz")
    partitions.append(data_temp)
# partitions = [(x_train, y_train, x_test, y_test), ...]


n_features = data_temp[0].shape[1]
#print(n_features)


def mk_model() -> keras.Model:
    model = tf.keras.models.Sequential(
        [
            tf.keras.layers.Dense(64, activation="relu", input_shape=(n_features,)),
            tf.keras.layers.Dropout(0.5),
            tf.keras.layers.Dense(64, activation="relu"),
            tf.keras.layers.Dropout(0.5),
            tf.keras.layers.Dense(2, activation="softmax"),
        ]
    )

    model.compile(
        loss="categorical_crossentropy",
        optimizer="adam",
        metrics=["accuracy"],
    )
    return model


mk_model().summary()


def get_evaluate_fn(testset):
    """Return an evaluation function for server-side (i.e. centralized) evaluation."""
    x_test, y_test = testset

    # The `evaluate` function will be called after every round by the strategy
    def evaluate(
        server_round: int,
        parameters: flwr.common.NDArrays,
        config: dict[str, flwr.common.Scalar],
    ):
        if server_round == NUM_ROUNDS:
            # Save final model
            model = mk_model()
            model.set_weights(parameters) 
            model.save("fl_model.keras")
        
        return model.evaluate(x_test, y_test, verbose=cast(str, 0))
    return evaluate


def eval_model(parameters, testset):
    loss, metrics = get_evaluate_fn(testset)(0, parameters, {})
    return {"loss": loss} | metrics


class FlowerClient(flwr.client.NumPyClient):
    def __init__(self, x_train, y_train, x_test, y_test):
        self.X_train = x_train
        self.y_train = y_train
        self.x_test = x_test
        self.y_test = y_test

        self.model = mk_model()

    def get_parameters(self, config):
        return self.model.get_weights()

    def fit(self, parameters, config):
        self.model.set_weights(parameters)
        self.model.fit(
            self.X_train,
            self.y_train,
            epochs=1,
            batch_size=BATCH_SIZE,
            validation_split=0.1,
            verbose=cast(str, 0),
        )
        return self.model.get_weights(), len(self.X_train), {}
    
    def evaluate(self, parameters, config):
        self.model.set_weights(parameters)

        steps = config["val_steps"]

        model = mk_model()  # Construct the model
        model.set_weights(parameters)  # Update model with the latest parameters
        loss, _ = model.evaluate(self.x_test, self.y_test, verbose=cast(str, 0), steps=steps)

        inferences = model.predict(self.x_test, verbose=cast(str, 0))
        y_pred = np.argmax(np.round(inferences), axis=1)
        y_true = np.argmax(self.y_test, axis=1)

        cm = confusion_matrix(y_true, y_pred)
        tn, fp, fn, tp = cm.ravel()

        accuracy = (tn + tp) / (tn + fp + fn + tp)
        precision = tp / (tp + fp)
        recall = tp / (tp + fn)
        f1 = 2 * tp / (2 * tp + fp + fn)
        miss_rate = fn / (fn + tp)

        return (
            loss,
            len(self.x_test),
            {
                "accuracy": accuracy,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "miss_rate": miss_rate,
            },
        )
    

def mk_client_fn(partitions):
    """Return a function which creates a new FlowerClient for a given partition."""

    def client_fn(cid: str) -> FlowerClient:
        """Create a new FlowerClient for partition i."""
        x_train, y_train, x_test, y_test = partitions[int(cid)]

        return FlowerClient(x_train, y_train, x_test, y_test)  # , x_eval_cid, y_eval_cid)

    return client_fn

def evaluate_config(rnd: int):
    return {"val_steps": 10}

def evaluate_metrics_aggregation_fn(eval_metrics: list[int, dict[str, Scalar]]) -> dict[str, Scalar]:
    """Aggregate evaluation metrics."""
    weights = [m[0] for m in eval_metrics]
    eval_metrics = [m[1] for m in eval_metrics]
    accuracy = np.average([m["accuracy"] for m in eval_metrics], weights=weights)
    precision = np.average([m["precision"] for m in eval_metrics], weights=weights)
    recall = np.average([m["recall"] for m in eval_metrics], weights=weights)
    f1 = np.average([m["f1"] for m in eval_metrics], weights=weights)
    miss_rate = np.average([m["miss_rate"] for m in eval_metrics], weights=weights)

    print("avg_accuracy: ", accuracy)
    print("client accuracy: ", [m["accuracy"] for m in eval_metrics])

    return {"accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1, "miss_rate": miss_rate}



strategy = FedAvg(
    fraction_fit=1.0,  # Sample 100% of available clients for training
    fraction_evaluate=0.5,  # Disable the federated evaluation
    min_evaluate_clients=NUM_CLIENTS,
    min_fit_clients=NUM_CLIENTS,  # Always sample all clients
    min_available_clients=NUM_CLIENTS,
    # evaluate_fn=get_evaluate_fn(testset),  # global evaluation function
    evaluate_metrics_aggregation_fn=evaluate_metrics_aggregation_fn,
    on_evaluate_config_fn=evaluate_config,
    initial_parameters=ndarrays_to_parameters(mk_model().get_weights()),
)

# With a dictionary, you tell Flower's VirtualClientEngine that each
# client needs exclusive access to these many resources in order to run
client_resources = {
    "num_cpus": max(int((os.cpu_count() or 1) / NUM_CLIENTS), 1),
    # "num_cpus": 1,
    "num_gpus": 0.0,
    
}

# Start simulation
history = flwr.simulation.start_simulation(
    client_fn=mk_client_fn(partitions),
    num_clients=NUM_CLIENTS,
    config=flwr.server.ServerConfig(num_rounds=NUM_ROUNDS),
    strategy=strategy,
    client_resources=client_resources,
    metric_evaluation_target={"label": "accuracy", "value":0.88},
    actor_kwargs={
        "on_actor_init_fn": enable_tf_gpu_growth  # Enable GPU growth upon actor init.
    },
    ray_init_args={"num_gpus": len(tf.config.list_physical_devices("GPU"))},
)

# Save history
with open("history.json", "w") as f:
    f.write(str(history.metrics_distributed))