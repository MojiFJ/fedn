import json
import os
import queue
import tempfile
import time
import uuid
import sys

import fedn.common.net.grpc.fedn_pb2 as fedn
from fedn.utils.helpers import get_helper
from fedn.aggregators.aggregator import AggregatorBase

class FedAvgAggregator(AggregatorBase):
    """ Local SGD / Federated Averaging (FedAvg) aggregator. 

    """

    def __init__(self, id, storage, server, modelservice, control):

        self.storage = storage
        self.id = id
        self.server = server
        self.modelservice = modelservice
        self.control = control

        self.validations = {}
        self.model_updates = queue.Queue()

    def on_model_update(self, model_id):
        """ Callback when a new model update is recieved from a client.
            Performs (optional) pre-processing and the puts the update id
            on the aggregation queue. 
        
        :param model_id: ID of model update (str)
        :return:
        """
        try:
            self.server.report_status("AGGREGATOR: callback received model {}".format(model_id),
                               log_level=fedn.Status.INFO)

            # Push the model update to the processing queue
            self.model_updates.put(model_id)
        except Exception as e:
            self.server.report_status("AGGREGATOR: Failed to receive candidate model! {}".format(e),
                               log_level=fedn.Status.WARNING)
            pass

    def on_model_validation(self, validation):
        """ Callback when a new model validation is recieved from a client. 

        :param validation: Dict containing validation data sent by client. 
                           Must be valid JSON.   
        :return:
        """

        # Currently, the validations are actually sent as status messages 
        # directly in the client, so here we are just storing them in the
        # combiner memory. This will need to be refactored later so that this 
        # callback is responsible for reporting the validation to the db. 

        model_id = validation.model_id
        data = json.loads(validation.data)
        try:
            self.validations[model_id].append(data)
        except KeyError:
            self.validations[model_id] = [data]

        self.server.report_status("AGGREGATOR: callback processed validation {}".format(validation.model_id),
                           log_level=fedn.Status.INFO)


    def combine_models(self, nr_expected_models=None, nr_required_models=1, helper=None, timeout=180):
        """ Compute a running average of model updates.

        :param nr_expected_models: The number of updates expected in this round.
        :param nr_required_models: The number of updates needed to a valid round.
        :param helper: An instance of the ML framework specific helper
        :param timeout: The maximum time waiting for model updates before returning 
                        the aggregated model. 
                    
        :return model,data: Tuple with the aggregated model and the performance metadata.
        """

        
        data = {}
        data['time_model_load'] = 0.0
        data['time_model_aggregation'] = 0.0

        self.server.report_status("COMBINER: Aggregating model updates...")

        round_time = 0.0
        polling_interval = 1.0
        nr_processed_models = 0
        while nr_processed_models < nr_expected_models:
            try:
                model_id = self.model_updates.get(block=False)
                self.server.report_status("Received model update with id {}".format(model_id))

                # Load the model update from disk
                tic = time.time()
                model_str = self.control.load_model_fault_tolerant(model_id)
                if model_str:
                    try:
                        model_next = helper.load_model_from_BytesIO(model_str.getbuffer())
                    except IOError:
                        self.server.report_status("COMBINER: Failed to load model!")
                else: 
                    raise
                data['time_model_load'] += time.time() - tic

                # Aggregate / reduce 
                tic = time.time()
                if nr_processed_models == 0:
                    model = model_next
                else:
                    model = helper.increment_average(model, model_next, nr_processed_models + 1)
                data['time_model_aggregation'] += time.time() - tic

                nr_processed_models += 1
                self.model_updates.task_done()
            except queue.Empty:
                self.server.report_status("AGGREGATOR: waiting for model updates: {} of {} completed.".format(nr_processed_models
                                                                                                     ,
                                                                                                     nr_expected_models))
                time.sleep(polling_interval)
                round_time += polling_interval
            except Exception as e:
                self.server.report_status("AGGERGATOR: Error encoutered while reading model update, skipping this update. {}".format(e))
                nr_expected_models -= 1
                if nr_expected_models <= 0:
                    return None, data
                self.model_updates.task_done()
           
            if round_time >= timeout:
                self.server.report_status("AGGREGATOR: training round timed out.", log_level=fedn.Status.WARNING)
                # TODO: Generalize policy for what to do in case of timeout. 
                if nr_processed_models >= nr_required_models:
                    break
                else:
                    return None, data

        data['nr_successful_updates'] = nr_processed_models

        self.server.report_status("AGGREGATOR: Training round completed, aggregated {} models.".format(nr_processed_models),
                           log_level=fedn.Status.INFO)
        return model, data
