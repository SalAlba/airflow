#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import datetime
import os
from typing import FrozenSet, Optional, Union

from sqlalchemy import func

from airflow.exceptions import AirflowException
from airflow.models import DagBag, DagModel, DagRun, TaskInstance
from airflow.operators.dummy_operator import DummyOperator
from airflow.sensors.base_sensor_operator import BaseSensorOperator
from airflow.utils.decorators import apply_defaults
from airflow.utils.session import provide_session
from airflow.utils.state import State


class ExternalTaskSensor(BaseSensorOperator):
    """
    Waits for a different DAG or a task in a different DAG to complete for a
    specific execution_date

    :param external_dag_id: The dag_id that contains the task you want to
        wait for
    :type external_dag_id: str
    :param external_task_id: The task_id that contains the task you want to
        wait for. If ``None`` (default value) the sensor waits for the DAG
    :type external_task_id: str or None
    :param allowed_states: list of allowed states, default is ``['success']``
    :type allowed_states: list
    :param failed_states: list of failed or dis-allowed states, default is ``None``
    :type failed_states: list
    :param execution_delta: time difference with the previous execution to
        look at, the default is the same execution_date as the current task or DAG.
        For yesterday, use [positive!] datetime.timedelta(days=1). Either
        execution_delta or execution_date_fn can be passed to
        ExternalTaskSensor, but not both.
    :type execution_delta: Optional[datetime.timedelta]
    :param execution_date_fn: function that receives the current execution date
        and returns the desired execution dates to query. Either execution_delta
        or execution_date_fn can be passed to ExternalTaskSensor, but not both.
    :type execution_date_fn: Optional[Callable]
    :param check_existence: Set to `True` to check if the external task exists (when
        external_task_id is not None) or check if the DAG to wait for exists (when
        external_task_id is None), and immediately cease waiting if the external task
        or DAG does not exist (default value: False).
    :type check_existence: bool
    """
    template_fields = ['external_dag_id', 'external_task_id']
    ui_color = '#19647e'

    @apply_defaults
    def __init__(self, *,
                 external_dag_id,
                 external_task_id=None,
                 allowed_states=None,
                 failed_states=None,
                 execution_delta=None,
                 execution_date_fn=None,
                 check_existence=False,
                 **kwargs):
        super().__init__(**kwargs)
        self.allowed_states = allowed_states or [State.SUCCESS]
        self.failed_states = failed_states or []

        total_states = self.allowed_states + self.failed_states
        total_states = set(total_states)

        if set(self.failed_states).intersection(set(self.allowed_states)):
            raise AirflowException("Duplicate values provided as allowed "
                                   "`{}` and failed states `{}`"
                                   .format(self.allowed_states, self.failed_states))

        if external_task_id:
            if not total_states <= set(State.task_states):
                raise ValueError(
                    f'Valid values for `allowed_states` and `failed_states` '
                    f'when `external_task_id` is not `None`: {State.task_states}'
                )
        elif not total_states <= set(State.dag_states):
            raise ValueError(
                f'Valid values for `allowed_states` and `failed_states` '
                f'when `external_task_id` is `None`: {State.dag_states}'
            )

        if execution_delta is not None and execution_date_fn is not None:
            raise ValueError(
                'Only one of `execution_delta` or `execution_date_fn` may '
                'be provided to ExternalTaskSensor; not both.')

        self.execution_delta = execution_delta
        self.execution_date_fn = execution_date_fn
        self.external_dag_id = external_dag_id
        self.external_task_id = external_task_id
        self.check_existence = check_existence
        # we only check the existence for the first time.
        self.has_checked_existence = False

    @provide_session
    def poke(self, context, session=None):
        if self.execution_delta:
            dttm = context['execution_date'] - self.execution_delta
        elif self.execution_date_fn:
            dttm = self._handle_execution_date_fn(context=context)
        else:
            dttm = context['execution_date']

        dttm_filter = dttm if isinstance(dttm, list) else [dttm]
        serialized_dttm_filter = ','.join(
            [datetime.isoformat() for datetime in dttm_filter])

        self.log.info(
            'Poking for %s.%s on %s ... ',
            self.external_dag_id, self.external_task_id, serialized_dttm_filter
        )

        DM = DagModel
        # we only do the check for 1st time, no need for subsequent poke
        if self.check_existence and not self.has_checked_existence:
            dag_to_wait = session.query(DM).filter(
                DM.dag_id == self.external_dag_id
            ).first()

            if not dag_to_wait:
                raise AirflowException(f'The external DAG {self.external_dag_id} does not exist.')
            elif not os.path.exists(dag_to_wait.fileloc):
                raise AirflowException(f'The external DAG {self.external_dag_id} was deleted.')

            if self.external_task_id:
                refreshed_dag_info = DagBag(dag_to_wait.fileloc).get_dag(self.external_dag_id)
                if not refreshed_dag_info.has_task(self.external_task_id):
                    raise AirflowException(
                        f'The external task {self.external_task_id} in '
                        f'DAG {self.external_dag_id} does not exist.'
                    )
            self.has_checked_existence = True

        count_allowed = self.get_count(dttm_filter, session, self.allowed_states)

        count_failed = -1
        if len(self.failed_states) > 0:
            count_failed = self.get_count(dttm_filter, session, self.failed_states)

        session.commit()
        if count_failed == len(dttm_filter):
            if self.external_task_id:
                raise AirflowException(
                    f'The external task {self.external_task_id} in DAG {self.external_dag_id} failed.'
                )
            else:
                raise AirflowException(f'The external DAG {self.external_dag_id} failed.')

        return count_allowed == len(dttm_filter)

    def get_count(self, dttm_filter, session, states):
        """
        Get the count of records against dttm filter and states

        :param dttm_filter: date time filter for execution date
        :type dttm_filter: list
        :param session: airflow session object
        :type session: SASession
        :param states: task or dag states
        :type states: list
        :return: count of record against the filters
        """
        TI = TaskInstance
        DR = DagRun

        if self.external_task_id:
            # .count() is inefficient
            count = session.query(func.count()).filter(
                TI.dag_id == self.external_dag_id,
                TI.task_id == self.external_task_id,
                TI.state.in_(states),  # pylint: disable=no-member
                TI.execution_date.in_(dttm_filter),
            ).scalar()
        else:
            # .count() is inefficient
            count = session.query(func.count()).filter(
                DR.dag_id == self.external_dag_id,
                DR.state.in_(states),  # pylint: disable=no-member
                DR.execution_date.in_(dttm_filter),
            ).scalar()
        return count

    def _handle_execution_date_fn(self, context):
        """
        This function is to handle backwards compatibility with how this operator was
        previously where it only passes the execution date, but also allow for the newer
        implementation to pass all context through as well, to allow for more sophisticated
        returns of dates to return.
        Namely, this function check the number of arguments in the execution_date_fn
        signature and if its 1, treat the legacy way, if it's 2, pass the context as
        the 2nd argument, and if its more, throw an exception.
        """
        num_fxn_params = self.execution_date_fn.__code__.co_argcount
        if num_fxn_params == 1:
            return self.execution_date_fn(context['execution_date'])
        if num_fxn_params == 2:
            return self.execution_date_fn(context['execution_date'], context)

        raise AirflowException(
            f'execution_date_fn passed {num_fxn_params} args but only allowed up to 2'
        )


class ExternalTaskMarker(DummyOperator):
    """
    Use this operator to indicate that a task on a different DAG depends on this task.
    When this task is cleared with "Recursive" selected, Airflow will clear the task on
    the other DAG and its downstream tasks recursively. Transitive dependencies are followed
    until the recursion_depth is reached.

    :param external_dag_id: The dag_id that contains the dependent task that needs to be cleared.
    :type external_dag_id: str
    :param external_task_id: The task_id of the dependent task that needs to be cleared.
    :type external_task_id: str
    :param execution_date: The execution_date of the dependent task that needs to be cleared.
    :type execution_date: str or datetime.datetime
    :param recursion_depth: The maximum level of transitive dependencies allowed. Default is 10.
        This is mostly used for preventing cyclic dependencies. It is fine to increase
        this number if necessary. However, too many levels of transitive dependencies will make
        it slower to clear tasks in the web UI.
    """
    template_fields = ['external_dag_id', 'external_task_id', 'execution_date']
    ui_color = '#19647e'

    # The _serialized_fields are lazily loaded when get_serialized_fields() method is called
    __serialized_fields: Optional[FrozenSet[str]] = None

    @apply_defaults
    def __init__(self, *,
                 external_dag_id,
                 external_task_id,
                 execution_date: Optional[Union[str, datetime.datetime]] = "{{ execution_date.isoformat() }}",
                 recursion_depth: int = 10,
                 **kwargs):
        super().__init__(**kwargs)
        self.external_dag_id = external_dag_id
        self.external_task_id = external_task_id
        if isinstance(execution_date, datetime.datetime):
            self.execution_date = execution_date.isoformat()
        elif isinstance(execution_date, str):
            self.execution_date = execution_date
        else:
            raise TypeError('Expected str or datetime.datetime type for execution_date. Got {}'
                            .format(type(execution_date)))
        if recursion_depth <= 0:
            raise ValueError("recursion_depth should be a positive integer")
        self.recursion_depth = recursion_depth

    @classmethod
    def get_serialized_fields(cls):
        """Serialized ExternalTaskMarker contain exactly these fields + templated_fields ."""
        if not cls.__serialized_fields:
            cls.__serialized_fields = frozenset(
                super().get_serialized_fields() | {
                    "recursion_depth"
                }
            )
        return cls.__serialized_fields
