# Copyright 2019 Toyota Research Institute. All rights reserved.
"""
Module for generating maccor procedure files from
input parameters and procedure templates
"""

import os
import warnings
import json
import time
import datetime
import csv
from copy import deepcopy

import pandas as pd
import numpy as np
import xmltodict
from docopt import docopt
from monty.serialization import loadfn
from beep import logger, __version__
from beep.protocol import PROCEDURE_TEMPLATE_DIR
from beep.conversion_schemas import MACCOR_WAVEFORM_CONFIG
from beep.utils import KinesisEvents, DashOrderedDict
s = {'service': 'ProtocolGenerator'}


class Procedure(DashOrderedDict):
    """
    Procedure file object. Provides factory methods
    to read a Maccor-type procedure file and invoke
    from templates for specific experimental
    procedure parameters

    """
    @classmethod
    def from_file(cls, filename, encoding='UTF-8'):
        """
        Procedure file ingestion. Invokes Procedure object
        from standard Maccor xml file.

        Args:
            filename (str): xml procedure file.

        Returns:
            (Procedure): Ordered dictionary with keys corresponding to options or
                control variables. Section headers are nested dicts or lists
                within the dict.
        """
        with open(filename, 'rb') as f:
            text = f.read().decode(encoding)
        data = xmltodict.parse(text, process_namespaces=False, strip_whitespace=True)
        return cls(data)

    # TODO: check on the necessity of this with MACCOR instrument
    def _format_maccor(self):
        """
        Dictionary reformatting of the entries in the procedure in
        order to match the maccor formats. Mainly re-adding whitespace
        to entries that were stripped on injestion.

        Returns:
            dict: Ordered dictionary with reformatted entries to match the
                formatting used in the maccor procedure files.
        """
        formatted = deepcopy(self)
        for step in formatted['MaccorTestProcedure']['ProcSteps']['TestStep']:
            # print(json.dumps(step['StepType'], indent=2))
            while len(step['StepType']) < 8:
                step['StepType'] = step['StepType'].center(8)
            if step['StepMode'] is None:
                step['StepMode'] = " "
            while len(step['StepMode']) < 8:
                step['StepMode'] = step['StepMode'].center(8)
            if step['Ends'] is not None:
                # If the Ends Element is a list we need to
                # check each entry in the list
                if isinstance(step['Ends']['EndEntry'], list):
                    # print(json.dumps(step['Ends'], indent=2))
                    for end_entry in step['Ends']['EndEntry']:
                        self.ends_whitespace(end_entry)
                if isinstance(step['Ends']['EndEntry'], dict):
                    self.ends_whitespace(step['Ends']['EndEntry'])
            if step['Reports'] is not None:
                if isinstance(step['Reports']['ReportEntry'], list):
                    for rep_entry in step['Reports']['ReportEntry']:
                        self.reports_whitespace(rep_entry)
                if isinstance(step['Reports']['ReportEntry'], dict):
                    self.reports_whitespace(step['Reports']['ReportEntry'])

        return formatted

    @staticmethod
    def ends_whitespace(end_entry):
        if end_entry['SpecialType'] is None:
            end_entry['SpecialType'] = " "
        while len(end_entry['EndType']) < 8:
            end_entry['EndType'] = end_entry['EndType'].center(8)
        if end_entry['Oper'] is not None:
            if len(end_entry['Oper']) < 2:
                end_entry['Oper'] = end_entry['Oper'].center(3)
            else:
                end_entry['Oper'] = end_entry['Oper'].ljust(3)

    @staticmethod
    def reports_whitespace(rep_entry):
        while len(rep_entry['ReportType']) < 8:
            rep_entry['ReportType'] = rep_entry['ReportType'].center(8)

    def to_file(self, filename, encoding='UTF-8'):
        """
        Writes object to maccor-formatted xml file using xmltodict
        unparse function.

        encoding (str): text encoding of output file

        Args:
            filename (str):file name to save xml to.
        """
        formatted = self._format_maccor()
        contents = xmltodict.unparse(
            input_dict=formatted,
            output=None,
            encoding=encoding,
            short_empty_elements=False,
            pretty=True,
            newl="\n",
            indent="  ")

        # Manually inject processing instructions on line 2
        line0, remainder = contents.split('\n', 1)
        line1 = "<?maccor-application progid=\"Maccor Procedure File\"?>"
        contents = "\n".join([line0, line1, remainder])
        contents = self._fixup_empty_elements(contents)
        contents += "\n"
        with open(filename, 'w') as f:
            f.write(contents)

    @staticmethod
    def _fixup_empty_elements(text):
        """
        xml reformatting to match the empty elements that are used
        in the maccor procedure format. Writes directly back to the file
        and assumes that the empty elements to be replaced are all on a
        single line.

        Args:
            text (str): xml file raw text to be formatted

        """
        text = text.replace(r"<Limits></Limits>", "<Limits/>")
        text = text.replace(r"<Reports></Reports>", "<Reports/>")
        text = text.replace(r"<Ends></Ends>", "<Ends/>")
        return text

    def modify_step_value(self, step_num, step_type, step_value):
        """
        Modifies the procedure parameters to set a step value at at given step num and type.

        Args:
            step_num (int): step id at which to set value
            step_type (str): step type at which to set value
            step_value (str): value to set

        Returns:
            dict: modified proc_dict with set value
        """
        for step_idx, step in enumerate(self['MaccorTestProcedure']['ProcSteps']['TestStep']):
            if step_idx == step_num and step['StepType'] == step_type:
                step['StepValue'] = step_value
        return self

    @classmethod
    def from_exp(cls, cutoff_voltage, charge_rate, discharge_rate,
                 template=None):
        """
        Generates a procedure according to the EXP-style template.

        Args:
            cutoff_voltage (float): cutoff voltage for.
            charge_rate (float): charging C-rate in 1/h.
            discharge_rate (float): discharging C-rate in 1/h.
            template (str): template name, defaults to EXP in template dir

        Returns:
            (Procedure): dictionary of procedure parameters.

        """
        # Load EXP template
        template = template or os.path.join(PROCEDURE_TEMPLATE_DIR, "EXP.000")
        obj = cls.from_file(template)

        # Modify according to params
        loop_idx_start, loop_idx_end = None, None
        for step_idx, step in enumerate(obj['MaccorTestProcedure']['ProcSteps']['TestStep']):
            if step['StepType'] == "Do 1":
                loop_idx_start = step_idx
            if step['StepType'] == "Loop 1":
                loop_idx_end = step_idx

        if loop_idx_start is None or loop_idx_end is None:
            raise UnboundLocalError("Loop index is not set")

        for step_idx, step in enumerate(obj['MaccorTestProcedure']['ProcSteps']['TestStep']):
            if step['StepType'] == 'Charge':
                if step['Limits'] is not None and 'Voltage' in step['Limits']:
                    step['Limits']['Voltage'] = cutoff_voltage
                if step['StepMode'] == 'Current' and loop_idx_start < step_idx < loop_idx_end:
                    step['StepValue'] = charge_rate
            if step['StepType'] == "Dischrge" and step['StepMode'] == 'Current' \
                    and loop_idx_start < step_idx < loop_idx_end:
                step['StepValue'] = discharge_rate

        return obj

    # TODO: rename this diagnosticv2 and merge
    @classmethod
    def from_regcyclev2(cls, reg_param, template=None):
        """
        Generates a procedure according to the diagnosticV2 template.

        Args:
            reg_param (pandas.Dataframe): containing the following quantities
                charge_constant_current_1 (float): C
                charge_percent_limit_1 (float): % of nominal capacity
                charge_constant_current_2 (float): C
                charge_cutoff_voltage (float): V
                charge_constant_voltage_time (integer): mins
                charge_rest_time (integer): mins
                discharge_constant_current (float): C
                discharge_cutoff_voltage (float): V
                discharge_rest_time (integer): mins
                cell_temperature_nominal (float): ˚C
                capacity_nominal (float): Ah
                diagnostic_start_cycle (integer): cycles
                diagnostic_interval (integer): cycles

        Returns:
            (Procedure): dictionary of procedure parameters.
        """

        assert reg_param['charge_cutoff_voltage'] > reg_param['discharge_cutoff_voltage']

        dc_idx = 1

        # Load template
        template = template or os.path.join(
            PROCEDURE_TEMPLATE_DIR, "diagnosticV2.000")
        obj = cls.from_file(template)
        obj.insert_resistance_regcyclev2(dc_idx, reg_param)

        # Start of initial set of regular cycles
        reg_charge_idx = 27 + 1
        obj.insert_charge_regcyclev2(reg_charge_idx, reg_param)
        reg_discharge_idx = 27 + 5
        obj.insert_discharge_regcyclev2(reg_discharge_idx, reg_param)

        # Start of main loop of regular cycles
        reg_charge_idx = 59 + 1
        obj.insert_charge_regcyclev2(reg_charge_idx, reg_param)
        reg_discharge_idx = 59 + 5
        obj.insert_discharge_regcyclev2(reg_discharge_idx, reg_param)

        # Storage cycle
        reg_storage_idx = 69
        obj.insert_storage_regcyclev2(reg_storage_idx, reg_param)

        return obj

    # TODO: These should probably all be private
    def insert_maccor_waveform_discharge(self, waveform_idx, waveform_filename):
        """
        Inserts a waveform into procedure dictionary at given id.

        Args:
            waveform_idx (int): Step in the procedure file to
                insert waveform at
            waveform_filename (str): Path to .MWF waveform file.
                Waveform needs to be pre-scaled for current/power
                capabilities of the cell and cycler

        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']

        self.set('MaccorTestProcedure.ProcSteps.TestStep.{}.StepType'.format(waveform_idx), 'FastWave')
        self.set('MaccorTestProcedure.ProcSteps.TestStep.{}.StepMode'.format(waveform_idx), '')
        self.set('MaccorTestProcedure.ProcSteps.TestStep.{}.Ends'.format(waveform_idx), None)
        self.set('MaccorTestProcedure.ProcSteps.TestStep.{}.Reports'.format(waveform_idx), None)
        self.set('MaccorTestProcedure.ProcSteps.TestStep.{}.Range'.format(waveform_idx), '')
        self.set('MaccorTestProcedure.ProcSteps.TestStep.{}.Option1'.format(waveform_idx), '')
        self.set('MaccorTestProcedure.ProcSteps.TestStep.{}.Option2'.format(waveform_idx), '')
        self.set('MaccorTestProcedure.ProcSteps.TestStep.{}.Option3'.format(waveform_idx), '')

        assert steps[waveform_idx]['StepType'] == "FastWave"
        assert waveform_filename.split('.')[-1].lower() == 'mwf'
        local_name = waveform_filename.split('.')[0]
        _, local_name = os.path.split(local_name)
        assert len(local_name) < 25, str(len(local_name))

        steps[waveform_idx]['StepValue'] = local_name

        return self

    def insert_resistance_regcyclev2(self, resist_idx, reg_param):
        """
        Inserts resistance into procedure dictionary at given id.

        Args:
            resist_idx (int):
            reg_param (pandas.DataFrame):

        Returns:
            dict:
        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']

        # Initial resistance check
        assert steps[resist_idx]['StepType'] == "Charge"
        assert steps[resist_idx]['StepMode'] == "Current"
        steps[resist_idx]['StepValue'] = float(round(1.0 * reg_param['capacity_nominal'], 3))

        return self

    def insert_charge_regcyclev2(self, charge_idx, reg_param):
        """
        Inserts charge into procedure dictionary at given id.

        Args:
            charge_idx (int)
            reg_param (pandas.DataFrame):

        Returns:
            dict:
        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']

        # Regular cycle constant current charge part 1
        step_idx = charge_idx
        assert steps[step_idx]['StepType'] == "Charge"
        assert steps[step_idx]['StepMode'] == "Current"
        steps[step_idx]['StepValue'] = float(round(reg_param['charge_constant_current_1']
                                                   * reg_param['capacity_nominal'], 3))
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(3600 * (reg_param['charge_percent_limit_1'] / 100)
                           / reg_param['charge_constant_current_1']))
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime(
            '%H:%M:%S', time.gmtime(time_s))

        # Regular cycle constant current charge part 2
        step_idx = charge_idx + 1
        assert steps[step_idx]['StepType'] == "Charge"
        assert steps[step_idx]['StepMode'] == "Current"
        steps[step_idx]['StepValue'] = float(round(reg_param['charge_constant_current_2']
                                                   * reg_param['capacity_nominal'], 3))
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "Voltage"
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = float(round(reg_param['charge_cutoff_voltage'], 3))

        # Regular cycle constant voltage hold
        step_idx = charge_idx + 2
        assert steps[step_idx]['StepType'] == "Charge"
        assert steps[step_idx]['StepMode'] == "Voltage"
        steps[step_idx]['StepValue'] = reg_param['charge_cutoff_voltage']
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(60 * reg_param['charge_constant_voltage_time']))
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        # Regular cycle rest at top of charge
        step_idx = charge_idx + 3
        assert steps[step_idx]['StepType'] == "Rest"
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(60 * reg_param['charge_rest_time']))
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        return self

    def insert_discharge_regcyclev2(self, discharge_idx, reg_param):
        """
        Inserts discharge into procedure dictionary at given id.

        Args:
            discharge_idx (int):
            reg_param (pandas.DataFrame):

        Returns:
            dict:
        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']

        # Regular cycle constant current discharge part 1
        step_idx = discharge_idx
        assert steps[step_idx]['StepType'] == "Dischrge"
        assert steps[step_idx]['StepMode'] == "Current"
        steps[step_idx]['StepValue'] = float(round(reg_param['discharge_constant_current']
                                                   * reg_param['capacity_nominal'], 3))
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "Voltage"
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = float(round(reg_param['discharge_cutoff_voltage'], 3))

        # Regular cycle rest after discharge
        step_idx = discharge_idx + 1
        assert steps[step_idx]['StepType'] == "Rest"
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(60 * reg_param['discharge_rest_time']))
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        # Regular cycle number of times to repeat regular cycle for initial offset and main body
        step_idx = discharge_idx + 3
        assert steps[step_idx]['StepType'][0:4] == "Loop"
        if steps[step_idx]['StepType'] == "Loop 1":
            assert steps[step_idx]['Ends']['EndEntry']['EndType'] == "Loop Cnt"
            steps[step_idx]['Ends']['EndEntry']['Value'] = reg_param['diagnostic_start_cycle']
        elif steps[step_idx]['StepType'] == "Loop 2":
            assert steps[step_idx]['Ends']['EndEntry']['EndType'] == "Loop Cnt"
            steps[step_idx]['Ends']['EndEntry']['Value'] = reg_param['diagnostic_interval']

        return self

    def insert_storage_regcyclev2(self, storage_idx, reg_param):
        """
        Inserts storage into procedure dictionary at given id.

        Args:
            storage_idx (int):
            reg_param (pandas.DataFrame):

        Returns:
            dict:
        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']

        # Storage condition
        step_idx = storage_idx
        assert steps[step_idx]['StepType'] == "Dischrge"
        assert steps[step_idx]['StepMode'] == "Current"
        steps[step_idx]['StepValue'] = float(round(0.5 * reg_param['capacity_nominal'], 3))
        steps[step_idx]['Limits']['Voltage'] = float(round(reg_param['discharge_cutoff_voltage'], 3))
        assert steps[step_idx]['Ends']['EndEntry']['EndType'] == "Current"
        steps[step_idx]['Ends']['EndEntry']['Value'] = float(round(0.05 * reg_param['capacity_nominal'], 3))
        step_idx = storage_idx + 1
        assert steps[step_idx]['StepType'] == "Charge"
        assert steps[step_idx]['StepMode'] == "Current"
        steps[step_idx]['StepValue'] = float(round(0.5 * reg_param['capacity_nominal'], 3))
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(60 * 12))
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        return self

    @classmethod
    def generate_procedure_regcyclev3(cls, protocol_index, reg_param, template=None):
        """
        Generates a procedure according to the diagnosticV3 template.

        Args:
            protocol_index (int): number of the protocol file being
                generated from this file.
            reg_param (pandas.DataFrame): containing the following quantities
                charge_constant_current_1 (float): C
                charge_percent_limit_1 (float): % of nominal capacity
                charge_constant_current_2 (float): C
                charge_cutoff_voltage (float): V
                charge_constant_voltage_time (integer): mins
                charge_rest_time (integer): mins
                discharge_constant_current (float): C
                discharge_cutoff_voltage (float): V
                discharge_rest_time (integer): mins
                cell_temperature_nominal (float): ˚C
                capacity_nominal (float): Ah
                diagnostic_start_cycle (integer): cycles
                diagnostic_interval (integer): cycles
            template (str): template for invocation, defaults to
                the diagnosticV3.000 template

        Returns:
            (Procedure): dictionary invoked using template/parameters
        """
        assert reg_param['charge_cutoff_voltage'] > reg_param['discharge_cutoff_voltage']
        assert reg_param['charge_constant_current_1'] <= reg_param['charge_constant_current_2']

        rest_idx = 0

        template = template or os.path.join(PROCEDURE_TEMPLATE_DIR, "diagnosticV3.000")
        obj = cls.from_file(template)
        obj.insert_initialrest_regcyclev3(rest_idx, protocol_index)

        dc_idx = 1
        obj.insert_resistance_regcyclev2(dc_idx, reg_param)

        # Start of initial set of regular cycles
        reg_charge_idx = 27 + 1
        obj.insert_charge_regcyclev3(reg_charge_idx, reg_param)
        reg_discharge_idx = 27 + 5
        obj.insert_discharge_regcyclev2(reg_discharge_idx, reg_param)

        # Start of main loop of regular cycles
        reg_charge_idx = 59 + 1
        obj.insert_charge_regcyclev3(reg_charge_idx, reg_param)
        reg_discharge_idx = 59 + 5
        obj.insert_discharge_regcyclev2(reg_discharge_idx, reg_param)

        # Storage cycle
        reg_storage_idx = 93
        obj.insert_storage_regcyclev3(reg_storage_idx, reg_param)

        # Check that the upper charge voltage is set the same for both charge current steps
        reg_charge_idx = 27 + 1
        assert obj['MaccorTestProcedure']['ProcSteps']['TestStep'][reg_charge_idx][
                   'Ends']['EndEntry'][1]['Value'] \
            == obj['MaccorTestProcedure']['ProcSteps']['TestStep'][reg_charge_idx + 1
               ]['Ends']['EndEntry'][0]['Value']

        reg_charge_idx = 59 + 1
        assert obj['MaccorTestProcedure']['ProcSteps']['TestStep'][reg_charge_idx
               ]['Ends']['EndEntry'][1]['Value'] \
               == obj['MaccorTestProcedure']['ProcSteps']['TestStep'][reg_charge_idx + 1
                                                                       ]['Ends']['EndEntry'][0]['Value']

        return obj

    def insert_initialrest_regcyclev3(self, rest_idx, index):
        """
        Inserts initial rest into procedure dictionary at given id.

        Args:
            rest_idx (int):
            index (int):

        Returns:
            dict:

        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']
        # Initial rest
        offset_seconds = 720
        assert steps[rest_idx]['StepType'] == "Rest"
        assert steps[rest_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(3 * 3600 + offset_seconds * (index % 96)))
        steps[rest_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        return self

    def insert_charge_regcyclev3(self, charge_idx, reg_param):
        """
        Inserts charge into procedure dictionary at given id.

        Args:
            charge_idx (int):
            reg_param (pandas.DataFrame):

        Returns:
            dict:
        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']

        # Regular cycle constant current charge part 1
        step_idx = charge_idx
        assert steps[step_idx]['StepType'] == "Charge"
        assert steps[step_idx]['StepMode'] == "Current"
        steps[step_idx]['StepValue'] = float(round(reg_param['charge_constant_current_1']
                                                     * reg_param['capacity_nominal'], 3))
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(3600 * (reg_param['charge_percent_limit_1'] / 100)
                           / reg_param['charge_constant_current_1']))
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))
        assert steps[step_idx]['Ends']['EndEntry'][1]['Value'] != 4.4
        steps[step_idx]['Ends']['EndEntry'][1]['Value'] = float(round(reg_param['charge_cutoff_voltage'], 3))

        # Regular cycle constant current charge part 2
        step_idx = charge_idx + 1
        assert steps[step_idx]['StepType'] == "Charge"
        assert steps[step_idx]['StepMode'] == "Current"
        steps[step_idx]['StepValue'] = float(round(reg_param['charge_constant_current_2']
                                                         * reg_param['capacity_nominal'], 3))
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "Voltage"
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = float(round(reg_param['charge_cutoff_voltage'], 3))

        # Regular cycle constant voltage hold
        step_idx = charge_idx + 2
        assert steps[step_idx]['StepType'] == "Charge"
        assert steps[step_idx]['StepMode'] == "Voltage"
        steps[step_idx]['StepValue'] = reg_param['charge_cutoff_voltage']
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(60 * reg_param['charge_constant_voltage_time']))
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        # Regular cycle rest at top of charge
        step_idx = charge_idx + 3
        assert steps[step_idx]['StepType'] == "Rest"
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(60 * reg_param['charge_rest_time']))
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        return self

    def insert_storage_regcyclev3(self, storage_idx, reg_param):
        """
        Inserts storage into procedure dictionary at given id.

        Args:
            self (dict):
            storage_idx (int):
            reg_param (pandas.DataFrame):

        Returns:
            dict:

        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']
        # Storage condition
        step_idx = storage_idx
        assert steps[step_idx]['StepType'] == "Dischrge"
        assert steps[step_idx]['StepMode'] == "Current"
        steps[step_idx]['StepValue'] = float(round(0.5 * reg_param['capacity_nominal'], 3))
        steps[step_idx]['Limits']['Voltage'] = float(round(reg_param['discharge_cutoff_voltage'], 3))
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "Current"
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = float(round(0.05 * reg_param['capacity_nominal'], 3))
        step_idx = storage_idx + 1
        assert steps[step_idx]['StepType'] == "Charge"
        assert steps[step_idx]['StepMode'] == "Current"
        steps[step_idx]['StepValue'] = float(round(0.5 * reg_param['capacity_nominal'], 3))
        assert steps[step_idx]['Ends']['EndEntry'][0]['EndType'] == "StepTime"
        time_s = int(round(60 * 12))
        steps[step_idx]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        return self

    # TODO: should this just be part of the general template?
    def add_procedure_diagcyclev2(self, nominal_capacity, diagnostic_params):
        """
        Modifies a procedure according to the diagnosticV2 template.

        Args:
            nominal_capacity (float): Standard capacity for this cell.
            diagnostic_params (pandas.Series): Series containing all of the
                diagnostic parameters.

        Returns:
            (dict) dictionary of procedure parameters.

        """
        start_reset_cycle_1 = 4
        self.insert_reset_cyclev2(start_reset_cycle_1, nominal_capacity, diagnostic_params)
        start_hppc_cycle_1 = 8
        self.insert_hppc_cyclev2(start_hppc_cycle_1, nominal_capacity, diagnostic_params)
        start_rpt_cycle_1 = 18
        self.insert_rpt_cyclev2(start_rpt_cycle_1, nominal_capacity, diagnostic_params)

        start_reset_cycle_2 = 37
        self.insert_reset_cyclev2(start_reset_cycle_2, nominal_capacity, diagnostic_params)
        start_hppc_cycle_2 = 40
        self.insert_hppc_cyclev2(start_hppc_cycle_2, nominal_capacity, diagnostic_params)
        start_rpt_cycle_2 = 50
        self.insert_rpt_cyclev2(start_rpt_cycle_2, nominal_capacity, diagnostic_params)

        return self

    # TODO: make private
    def generate_procedure_diagcyclev3(self, nominal_capacity, diagnostic_params):
        """
        Generates a diagnostic procedure according
        to the diagnosticV3 template.

        Args:
            nominal_capacity (float): Standard capacity for this cell.
            diagnostic_params (pandas.Series): Series containing all of the
                diagnostic parameters.

        Returns:
            dict: dictionary of procedure parameters.

        """
        start_reset_cycle_1 = 4
        self.insert_reset_cyclev2(start_reset_cycle_1, nominal_capacity, diagnostic_params)
        start_hppc_cycle_1 = 8
        self.insert_hppc_cyclev2(start_hppc_cycle_1, nominal_capacity, diagnostic_params)
        start_rpt_cycle_1 = 18
        self.insert_rpt_cyclev2(start_rpt_cycle_1, nominal_capacity, diagnostic_params)

        start_reset_cycle_2 = 37
        self.insert_reset_cyclev2(start_reset_cycle_2, nominal_capacity, diagnostic_params)
        start_hppc_cycle_2 = 40
        self.insert_hppc_cyclev2(start_hppc_cycle_2, nominal_capacity, diagnostic_params)
        start_rpt_cycle_2 = 50
        self.insert_rpt_cyclev2(start_rpt_cycle_2, nominal_capacity, diagnostic_params)

        start_reset_cycle_3 = 70
        self.insert_reset_cyclev2(start_reset_cycle_3, nominal_capacity, diagnostic_params)
        start_hppc_cycle_3 = 74
        self.insert_hppc_cyclev2(start_hppc_cycle_3, nominal_capacity, diagnostic_params)
        start_rpt_cycle_3 = 84
        self.insert_rpt_cyclev2(start_rpt_cycle_3, nominal_capacity, diagnostic_params)

        return self

    def insert_reset_cyclev2(self, start, nominal_capacity, diagnostic_params):
        """
        Helper function for parameterizing the reset cycle.

        Args:
            start:
            nominal_capacity:
            diagnostic_params:

        Returns:
            dict:
        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']
        # Charge step for reset cycle
        assert steps[start]['StepType'] == 'Charge'
        assert steps[start]['StepMode'] == 'Current'
        steps[start]['StepValue'] = float(round(nominal_capacity * diagnostic_params['reset_cycle_current'], 4))
        steps[start]['Limits']['Voltage'] = float(round(diagnostic_params['diagnostic_charge_cutoff_voltage'], 3))
        assert steps[start]['Ends']['EndEntry'][0]['EndType'] == 'Current'
        steps[start]['Ends']['EndEntry'][0]['Value'] = float(round(nominal_capacity *
                                                                   diagnostic_params['reset_cycle_cutoff_current'], 4))

        # Discharge step for reset cycle
        assert steps[start+1]['StepType'] == 'Dischrge'
        assert steps[start+1]['StepMode'] == 'Current'
        steps[start+1]['StepValue'] = float(round(nominal_capacity * diagnostic_params['reset_cycle_current'], 4))
        assert steps[start+1]['Ends']['EndEntry'][0]['EndType'] == 'Voltage'
        steps[start+1]['Ends']['EndEntry'][0]['Value'] = \
            float(round(diagnostic_params['diagnostic_discharge_cutoff_voltage'], 3))

        return self

    def insert_hppc_cyclev2(self, start, nominal_capacity, diagnostic_params):
        """
        Helper function for parameterizing the hybrid pulse power cycle

        Args:
            start:
            nominal_capacity:
            diagnostic_params:

        Returns:
            dict:
        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']
        # Initial charge step for hppc cycle
        assert steps[start]['StepType'] == 'Charge'
        assert steps[start]['StepMode'] == 'Current'
        steps[start]['StepValue'] = float(round(nominal_capacity *
                                                diagnostic_params['HPPC_baseline_constant_current'], 3))
        steps[start]['Limits']['Voltage'] = float(round(diagnostic_params['diagnostic_charge_cutoff_voltage'], 3))
        assert steps[start]['Ends']['EndEntry'][0]['EndType'] == 'Current'
        steps[start]['Ends']['EndEntry'][0]['Value'] = float(round(nominal_capacity *
                                                                   diagnostic_params['diagnostic_cutoff_current'], 3))

        # Rest step for hppc cycle
        assert steps[start+2]['StepType'] == 'Rest'
        assert steps[start+2]['Ends']['EndEntry'][0]['EndType'] == 'StepTime'
        time_s = int(round(60 * diagnostic_params['HPPC_rest_time']))
        steps[start+2]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        # Discharge step 1 for hppc cycle
        assert steps[start+3]['StepType'] == 'Dischrge'
        assert steps[start+3]['StepMode'] == 'Current'
        steps[start+3]['StepValue'] = float(round(nominal_capacity *
                                                  diagnostic_params['HPPC_pulse_current_1'], 3))
        assert steps[start+3]['Ends']['EndEntry'][0]['EndType'] == 'StepTime'
        time_s = diagnostic_params['HPPC_pulse_duration_1']
        steps[start+3]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))
        assert steps[start+3]['Ends']['EndEntry'][1]['EndType'] == 'Voltage'
        steps[start+3]['Ends']['EndEntry'][1]['Value'] = \
            float(round(diagnostic_params['diagnostic_discharge_cutoff_voltage'], 3))

        # Pulse rest step for hppc cycle
        assert steps[start+4]['StepType'] == 'Rest'
        assert steps[start+4]['Ends']['EndEntry'][0]['EndType'] == 'StepTime'
        time_s = int(round(diagnostic_params['HPPC_pulse_rest_time']))
        steps[start+4]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))

        # Charge step 1 for hppc cycle
        assert steps[start+5]['StepType'] == 'Charge'
        assert steps[start+5]['StepMode'] == 'Current'
        steps[start+5]['StepValue'] = float(round(nominal_capacity *
                                                  diagnostic_params['HPPC_pulse_current_2'], 3))
        assert steps[start+5]['Ends']['EndEntry'][0]['EndType'] == 'StepTime'
        time_s = diagnostic_params['HPPC_pulse_duration_2']
        steps[start+5]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))
        assert steps[start+5]['Ends']['EndEntry'][1]['EndType'] == 'Voltage'
        steps[start+5]['Ends']['EndEntry'][1]['Value'] = \
            float(round(diagnostic_params['HPPC_pulse_cutoff_voltage'], 3))

        # Discharge step 2 for hppc cycle
        assert steps[start+6]['StepType'] == 'Dischrge'
        assert steps[start+6]['StepMode'] == 'Current'
        steps[start+6]['StepValue'] = float(round(nominal_capacity *
                                                  diagnostic_params['HPPC_baseline_constant_current'], 3))
        assert steps[start+6]['Ends']['EndEntry'][0]['EndType'] == 'StepTime'
        time_s = int(round(3600 * (diagnostic_params['HPPC_interval'] / 100) /
                           diagnostic_params['HPPC_baseline_constant_current'] - 1))
        steps[start+6]['Ends']['EndEntry'][0]['Value'] = time.strftime('%H:%M:%S', time.gmtime(time_s))
        assert steps[start+6]['Ends']['EndEntry'][1]['EndType'] == 'Voltage'
        steps[start+6]['Ends']['EndEntry'][1]['Value'] = \
            float(round(diagnostic_params['diagnostic_discharge_cutoff_voltage'], 3))

        # Final discharge step for hppc cycle
        assert steps[start+8]['StepType'] == 'Dischrge'
        assert steps[start+8]['StepMode'] == 'Voltage'
        steps[start+8]['StepValue'] = float(round(diagnostic_params['diagnostic_discharge_cutoff_voltage'], 3))
        steps[start+8]['Limits']['Current'] = float(round(nominal_capacity *
                                                          diagnostic_params['HPPC_baseline_constant_current'], 3))
        assert steps[start+8]['Ends']['EndEntry'][0]['EndType'] == 'Current'
        steps[start+8]['Ends']['EndEntry'][0]['Value'] = float(round(nominal_capacity *
                                                       diagnostic_params['diagnostic_cutoff_current'], 3))

        return self

    def insert_rpt_cyclev2(self, start, nominal_capacity, diagnostic_params):
        """
        Helper function for parameterizing the Rate Performance Test cycle

        Args:
            start:
            nominal_capacity:
            diagnostic_params:

        Returns:
            dict
        """
        steps = self['MaccorTestProcedure']['ProcSteps']['TestStep']

        # First charge step for rpt cycle
        assert steps[start]['StepType'] == 'Charge'
        assert steps[start]['StepMode'] == 'Current'
        steps[start]['StepValue'] = float(round(nominal_capacity *
                                                diagnostic_params['RPT_charge_constant_current'], 3))
        steps[start]['Limits']['Voltage'] = float(round(diagnostic_params['diagnostic_charge_cutoff_voltage'], 3))
        assert steps[start]['Ends']['EndEntry'][0]['EndType'] == 'Current'
        steps[start]['Ends']['EndEntry'][0]['Value'] = float(round(nominal_capacity *
                                                                   diagnostic_params['diagnostic_cutoff_current'], 3))

        # 0.2C discharge step for rpt cycle
        assert steps[start+1]['StepType'] == 'Dischrge'
        assert steps[start+1]['StepMode'] == 'Current'
        steps[start+1]['StepValue'] = float(round(nominal_capacity *
                                                  diagnostic_params['RPT_discharge_constant_current_1'], 3))
        assert steps[start+1]['Ends']['EndEntry'][0]['EndType'] == 'Voltage'
        steps[start+1]['Ends']['EndEntry'][0]['Value'] = \
            float(round(diagnostic_params['diagnostic_discharge_cutoff_voltage'], 3))

        # Second charge step for rpt cycle
        assert steps[start+3]['StepType'] == 'Charge'
        assert steps[start+3]['StepMode'] == 'Current'
        steps[start+3]['StepValue'] = float(round(nominal_capacity *
                                                  diagnostic_params['RPT_charge_constant_current'], 3))
        steps[start+3]['Limits']['Voltage'] = float(round(diagnostic_params['diagnostic_charge_cutoff_voltage'], 3))
        assert steps[start+3]['Ends']['EndEntry'][0]['EndType'] == 'Current'
        steps[start+3]['Ends']['EndEntry'][0]['Value'] = float(round(nominal_capacity *
                                                                     diagnostic_params['diagnostic_cutoff_current'], 3))

        # 1C discharge step for rpt cycle
        assert steps[start+4]['StepType'] == 'Dischrge'
        assert steps[start+4]['StepMode'] == 'Current'
        steps[start+4]['StepValue'] = float(round(nominal_capacity *
                                                  diagnostic_params['RPT_discharge_constant_current_2'], 3))
        assert steps[start+4]['Ends']['EndEntry'][0]['EndType'] == 'Voltage'
        steps[start+4]['Ends']['EndEntry'][0]['Value'] = \
            float(round(diagnostic_params['diagnostic_discharge_cutoff_voltage'], 3))

        # Third charge step for rpt cycle
        assert steps[start+6]['StepType'] == 'Charge'
        assert steps[start+6]['StepMode'] == 'Current'
        steps[start+6]['StepValue'] = float(round(nominal_capacity *
                                                  diagnostic_params['RPT_charge_constant_current'], 3))
        steps[start+6]['Limits']['Voltage'] = float(round(diagnostic_params['diagnostic_charge_cutoff_voltage'], 3))
        assert steps[start+6]['Ends']['EndEntry'][0]['EndType'] == 'Current'
        steps[start+6]['Ends']['EndEntry'][0]['Value'] = float(round(nominal_capacity *
                                                                     diagnostic_params['diagnostic_cutoff_current'], 3))

        # 2C discharge step for rpt cycle
        assert steps[start+7]['StepType'] == 'Dischrge'
        assert steps[start+7]['StepMode'] == 'Current'
        steps[start+7]['StepValue'] = float(round(nominal_capacity *
                                                  diagnostic_params['RPT_discharge_constant_current_3'], 3))
        assert steps[start+7]['Ends']['EndEntry'][0]['EndType'] == 'Voltage'
        steps[start+7]['Ends']['EndEntry'][0]['Value'] = \
            float(round(diagnostic_params['diagnostic_discharge_cutoff_voltage'], 3))

        return self


def generate_maccor_waveform_file(df, file_prefix, file_directory, mwf_config=None):
    """
    Helper function that takes in a variable power waveform and outputs a maccor waveform file (.MWF), which is read by
    the cycler when the procedure file has a "fast-wave" step.
    Relevant parameters to generate the .mwf files governing the input mode, charge/discharge limits, end conditions
    and scaling are stored in /conversion_schemas/maccor_waveform_conversion.yaml

    Args:
        df (pd.DataFrame): power waveform containing two columns "time" and "power", in sec and W respectively.
        file_prefix (str): prefix for the filename (extension is .MWF by default)
        file_directory (str): folder to store the mwf file
        mwf_config (dict): dictionary of instrument control parameters for generating the waveform

    Returns:
         path to the maccor waveform file generated
    """

    if mwf_config is None:
        mwf_config = MACCOR_WAVEFORM_CONFIG

    df['power'] = df['power'] / max(abs(df['power'])) * mwf_config['value_scale']

    df['step_counter'] = df['power'].diff().fillna(0).ne(0).cumsum()
    df = df.groupby('step_counter').agg({'time': 'count',
                                         'power': 'first'})

    df.rename(columns={'time': 'duration'}, inplace=True)

    df['control_mode'] = mwf_config['control_mode']

    if mwf_config['control_mode'] == 'P':
        df.loc[df['power'] == 0, 'control_mode'] = 'I'

    df['value'] = np.round(abs(df['power']), 5)

    mask = df['power'] <= 0
    df = df.assign(**{'state': 'C',
                      'limit_mode': mwf_config['charge_limit_mode'],
                      'limit_value': mwf_config['charge_limit_value'],
                      'end_mode': mwf_config['charge_end_mode'],
                      'operation':  mwf_config['charge_end_operation'],
                      'end_mode_value': mwf_config['charge_end_mode_value']
                      })

    df.loc[mask, ['state', 'limit_mode', 'limit_value', 'end_mode', 'operation', 'end_mode_value']] =\
        ['D', mwf_config['discharge_limit_mode'], mwf_config['discharge_limit_value'],
         mwf_config['discharge_end_mode'], mwf_config['discharge_end_operation'],
         mwf_config['discharge_end_mode_value']]

    df['report_mode'] = mwf_config['report_mode']
    df['report_value'] = mwf_config['report_value']
    df['range'] = mwf_config['range']

    MWF_file_path = os.path.join(file_directory, file_prefix + ".MWF")

    df[['state', 'control_mode', 'value', 'limit_mode', 'limit_value', 'duration', 'end_mode',
        'operation', 'end_mode_value', 'report_mode', 'report_value', 'range']].\
        to_csv(MWF_file_path, sep='\t', header=None, index=None)

    return MWF_file_path
