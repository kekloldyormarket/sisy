'''
Created on Feb 12, 2017

@author: julien
@author: Charlie, 9-2017
'''
import logging
from multiprocessing import Queue, Process
from threading import Thread
from time import time
import traceback
from time import sleep
from keras.callbacks import ModelCheckpoint
import numpy

from minos.experiment.training import EpochStoppingCondition, \
    AccuracyDecreaseStoppingCondition, StoppingConditionWrapper, \
    get_associated_validation_metric, is_minimize_metric
from minos.tf_utils import setup_tf_session
from minos.utils import disable_sysout, load_keras_model, save_sisy_model


class MultiProcessModelTrainer(object):
    def __init__(self, batch_iterator, test_batch_iterator, environment):
        self.batch_iterator = batch_iterator
        self.test_batch_iterator = test_batch_iterator
        self.environment = environment

    def build_and_train_models(self, blueprints):
        logging.debug('Training %d models' % len(blueprints))
        return self._start_training_workers(blueprints)

    def _start_training_workers(self, blueprints):
        try:
            total_n_jobs = sum(self.environment.n_jobs)
            logging.info('total_n_jobs {}'.format(total_n_jobs))
            logging.info(self.environment.n_jobs)
            logging.info(self.environment.devices)
            work_queue = Queue(total_n_jobs)
            result_queue = Queue(total_n_jobs)
            
            print(self.environment.devices)
            self.processes = [
            Process(
                target=model_training_worker,
                args=(
                    self.batch_iterator,
                    self.test_batch_iterator,
                    device_id,
                    device,
                    work_queue,
                    result_queue,
                    _job
                ))
            for device_id, device in enumerate(self.environment.devices)
            for _job in range(self.environment.n_jobs[device_id])
            ]

            self.process_count = 0
            for process in self.processes:
                sleep(0.1)
                process.start()

            def _work_feeder():
                count = len(blueprints)
                for i, blueprint in enumerate(blueprints):
                    work_queue.put((i, count, blueprint))
                for _ in range(sum(self.environment.n_jobs)):
                    work_queue.put(None)

            Thread(target=_work_feeder).start()

            results = []
            while self.process_count > 0:
                result = result_queue.get()
                if result:
                    logging.info(
                        'Blueprint %d: best score %f, epoch %d/%d',
                        result[0],
                        result[1],
                        result[2],
                        result[3])
                    results.append(result)
                else:
                    self.process_count -= 1
            results = list(
                sorted(
                    results,
                    key=lambda e: e[0]))
            return results
        except Exception as ex:
            logging.error(ex)
            logging.error(traceback.format_exc())


class ModelTrainer(object):
    def __init__(self, batch_iterator, test_batch_iterator):
        from minos.model.build import ModelBuilder
        self.model_builder = ModelBuilder()
        self.batch_iterator = batch_iterator
        self.test_batch_iterator = test_batch_iterator

    def train(self, blueprint, device,
              save_best_model=False, model_filename=None, class_weight=None, verbose=2):
        try:
            model = self.model_builder.build(
                blueprint,
                device)
            class_weight = class_weight or blueprint.training.class_weight
            setup_tf_session(device)
            nb_epoch, callbacks = self._get_stopping_parameters(blueprint)
            if save_best_model:
                callbacks.append(self._get_model_save_callback(
                    model_filename,
                    blueprint.training.metric.metric))
            start = time()

            X_train = self.batch_iterator.X
            y_train = self.batch_iterator.y
            X_test = self.test_batch_iterator.X
            y_test = self.test_batch_iterator.y
            batch_size = self.batch_iterator.batch_size

            history = model.fit(X_train, y_train,
                                batch_size=batch_size,
                                epochs=nb_epoch,
                                validation_data=(X_test, y_test),
                                class_weight=class_weight,
                                verbose=verbose)

            if save_best_model:
                del model
                model = load_keras_model(model_filename)
            return model, history, (time() - start)
        except Exception as ex:
            logging.debug(ex)
            logging.debug(traceback.format_exc())
        try:
            from keras import backend
            backend.clear_session()
        except:
            logging.debug(ex)
            logging.debug(traceback.format_exc())
        return None, None, 0

    def _get_model_save_callback(self, model_filename, metric):
        checkpoint = ModelCheckpoint(
            model_filename,
            monitor=metric,
            save_best_only=True)
        return checkpoint

    def _get_stopping_parameters(self, blueprint):
        if isinstance(blueprint.training.stopping, EpochStoppingCondition):
            nb_epoch = blueprint.training.stopping.epoch
            stopping_callbacks = []
        if isinstance(blueprint.training.stopping, AccuracyDecreaseStoppingCondition):
            nb_epoch = max(
                1,
                blueprint.training.stopping.min_epoch,
                blueprint.training.stopping.max_epoch)
            stopping_callbacks = [
                StoppingConditionWrapper(blueprint.training.stopping)]
        return nb_epoch, stopping_callbacks



def model_training_worker(batch_iterator, test_batch_iterator,
                          device_id, device, work_queue, result_queue, step ):
    # disable_sysout()
    model_trainer = ModelTrainer(
        batch_iterator,
        test_batch_iterator)
    work = work_queue.get()
    best_score = 0

    while work:
        try:
            idx, _total, blueprint = work
            model, history, duration = model_trainer.train(
                blueprint,
                device)
            if model and history:
                epoch_total = len(history.epoch)
                val_metric = get_associated_validation_metric(blueprint.training.metric.metric)

                if is_minimize_metric(val_metric):
                    epoch_best = numpy.argmin(history.history[val_metric])
                    score = history.history[val_metric][epoch_best]
                else:
                    epoch_best = numpy.argmax(history.history[val_metric])
                    score = history.history[val_metric][epoch_best]
            else:
                score, epoch_best, epoch_total = 0, 0, 0
            result_queue.put((idx, score, epoch_best, epoch_total, blueprint, duration, device_id))
            work = work_queue.get()
            if score > best_score:
                best_score = score
                save_sisy_model(model,'testing',step)

        except Exception as ex:
            logging.error(ex)
            logging.error(traceback.format_exc())
    result_queue.put(None)
