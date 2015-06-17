# -*- coding: utf-8 -*-
# Copyright (c) 2013, Roboterclub Aachen e.V.
# All rights reserved.
#
# The file is part of the xpcc library and is released under the 3-clause BSD
# license. See the file `LICENSE` for the full license governing this code.
# -----------------------------------------------------------------------------

from reader import XMLDeviceReader
from peripheral import Peripheral
from register import Register
from lxml import etree

import os, sys, math, glob, re
# add python module logger to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'logger'))
from logger import Logger
# add python module device files to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'device_files'))
from device_identifier import DeviceIdentifier
from stm import stm32_defines
from stm import stm32f1_remaps
from stm import stm32_memory

class STMDeviceReader(XMLDeviceReader):
	""" STMDeviceReader
	This STM specific part description file reader knows the structure and
	translates the data into a platform independent format.
	"""
	familyFile = None
	rootpath = None

	@staticmethod
	def getDevicesFromFamily(family, logger=None, rootpath=None):
		if rootpath is None:
			rootpath = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'STM_devices')
		STMDeviceReader.rootpath = rootpath

		STMDeviceReader.familyFile = XMLDeviceReader(os.path.join(rootpath, 'families.xml'), logger)
		families = STMDeviceReader.familyFile.query("//Family[@Name='{}']/SubFamily/Mcu/@RefName".format(family))
		logger.debug("STMDeviceReader: Found devices of family '{}': {}".format(family, ", ".join(families)))
		return families


	def __init__(self, deviceName, logger=None):
		deviceNames = self.familyFile.query("//Family/SubFamily/Mcu[@RefName='{}']".format(deviceName))[0]
		comboDeviceName = deviceNames.get('Name')
		deviceFile = os.path.join(self.rootpath, comboDeviceName + '.xml')

		XMLDeviceReader.__init__(self, deviceFile, logger)
		self.name = deviceName
		self.id = DeviceIdentifier(self.name.lower())

		if logger:
			logger.info("STMDeviceReader: Parsing '{}'".format(self.id.string))

		# information about the core and architecture
		coreLut = {'m0': 'v6m', 'm3': 'v7m', 'm4': 'v7em'}
		core = self.query('//Core')[0].text.replace('ARM ', '').lower()
		self.addProperty('architecture', coreLut[core.replace('cortex-', '')])
		if core.endswith('m4'):
			core += 'f'
		self.addProperty('core', core)

		# flash and ram sizes
		# The <ram> and <flash> can occur multiple times.
		# they are "ordered" in the same way as the `(S-I-Z-E)` ids in the device combo name
		# we must first find out which index the current self.id.size_id has inside `(S-I-Z-E)`
		sizeIndex = 0

		matchString = "\(.(-.)*\)"
		match = re.search(matchString, comboDeviceName)
		if match:
			sizeArray = match.group(0)[1:-1].lower().split("-")
			sizeIndex = sizeArray.index(self.id.size_id)

		rams = self.query("//Ram")
		if len(rams) <= sizeIndex:
			sizeIndex = 0

		ram = int(rams[sizeIndex].text)
		flash = int(self.query("//Flash")[sizeIndex].text)
		self.addProperty('ram', ram)
		self.addProperty('flash', flash)

		mem_fam = stm32_memory[self.id.family]
		mem_model = None
		for model in mem_fam['model']:
			if self.id.name in model['names']:
				mem_model = model
				break
		if mem_model == None:
			self.log.error("STMDeviceReader: Memory model not found for device '{}'".format(self.id.string))

		memories = []
		# first get the real SRAM1 size
		for mem, val in mem_model['memories'].items():
			if '2' in mem or '3' in mem or 'ccm' in mem:
				ram -= val

		# add all memories
		for mem, val in mem_model['memories'].items():
			if '1' in mem:
				memories.append({'name': 'sram1',
								 'access' : 'rwx',
								 'start': "0x{:02X}".format(mem_fam['start']['sram']),
								 'size': str(ram)})
			elif '2' in mem:
				memories.append({'name': 'sram2',
								 'access' : 'rwx',
								 'start': "0x{:02X}".format(mem_fam['start']['sram'] + ram*1024),
								 'size': str(val)})
			elif '3' in mem:
				memories.append({'name': 'sram3',
								 'access': 'rwx',
								 'start': "0x{:02X}".format(mem_fam['start']['sram'] + ram * 1024 + mem_model['memories']['sram2'] * 1024),
								 'size': str(val)})
			elif 'flash' in mem:
				memories.append({'name': 'flash',
								 'access': 'rx',
								 'start': "0x{:02X}".format(mem_fam['start']['flash']),
								 'size': str(flash)})
			else:
				memories.append({'name': mem,
								 'access': 'rwx',
								 'start': "0x{:02X}".format(mem_fam['start'][mem]),
								 'size': str(val)})

		self.addProperty('memories', memories)

		# packaging
		package = self.query("//@Package")[0]
		self.addProperty('pin-count', re.findall('[0-9]+', package)[0])
		self.addProperty('package', re.findall('[A-Za-z\.]+', package)[0])

		# device header
		self.addProperty('header', 'stm32' + self.id.family + 'xx.h')

		# device defines
		define = self._getDeviceDefine()
		if define is None:
			logger.warn("STMDeviceReader: Define not found for device '{}'".format(self.id.string))
		else:
			self.addProperty('define', define)

		if self.id.family == 'f4':
			# required for our FreeRTOS
			self.addProperty('define', 'STM32F4XX')

		gpios = []
		self.addProperty('gpios', gpios)
		gpio_afs = []
		self.addProperty('gpio_afs', gpio_afs)
		peripherals = []
		self.addProperty('peripherals', peripherals)
		modules = []
		self.addProperty('modules', modules)

		self.modules = self.query("//IP/@InstanceName")
		self.modules = sorted(list(set(self.modules)))
		self.log.debug("Available Modules are:\n" + self._modulesToString())

		for m in self.modules:
			if any(m.startswith(per) for per in ['TIM', 'UART', 'USART', 'ADC', 'DAC', 'CAN', 'SPI', 'I2C', 'OTG', 'DMA', 'USB', 'FSMC']):
				modules.append(m)

		invertMode = {'out': 'in', 'in': 'out', 'io': 'io'}
		nameToMode = {'rx': 'in', 'tx': 'out', 'cts': 'in', 'rts': 'out', 'ck': 'out',	# Uart
					 'miso': 'in', 'mosi': 'out', 'nss': 'io', 'sck': 'out',	# Spi
					 'scl': 'out', 'sda': 'io'}	# I2c

		# lets load additional information about the GPIO IP
		ip_file = self.query("//IP[@Name='GPIO']")[0].get('Version')
		ip_file = os.path.join(self.rootpath, 'IP', 'GPIO-' + ip_file + '_Modes.xml')
		self.gpioFile = XMLDeviceReader(ip_file, logger)

		pins = self.query("//Pin[@Type='I/O'][starts-with(@Name,'P')]")
		pins = sorted(pins, key=lambda p: [p.get('Name')[1:2], int(p.get('Name')[:4].split('-')[0].split('/')[0][2:])])

		for pin in pins:
			name = pin.get('Name')
			# F1 does not have pin 'alternate functions' only pin 'remaps' which switch groups of pins
			if self.id.family == 'f1':
				pinSignals = self.gpioFile.compactQuery("//GPIO_Pin[@Name='{}']/PinSignal/RemapBlock/..".format(name))
				rawAltFunctions = {a.get('Name'): a[0].get('Name')[-1:] for a in pinSignals}
				altFunctions = {}
				for alt_name in rawAltFunctions:
					key = alt_name.split('_')[0].lower()
					if key not in stm32f1_remaps:
						key += alt_name.split('_')[1].lower()
					if key in stm32f1_remaps:
						mask = stm32f1_remaps[key]['mask']
						pos = stm32f1_remaps[key]['position']
						value = stm32f1_remaps[key]['mapping'][int(rawAltFunctions[alt_name])]
						altFunctions[alt_name] = '{},{},{}'.format(pos, mask, value)
					else:
						altFunctions[alt_name] = '-1'



			else:	# F0, F3 and F4
				pinSignals = self.gpioFile.compactQuery("//GPIO_Pin[@Name='%s']/PinSignal/SpecificParameter[@Name='GPIO_AF']/.." % name)
				altFunctions = { a.get('Name') : a[0][0].text.replace('GPIO_AF', '')[:2].replace('_','') for a in pinSignals }

			# the analog channels are only available in the Mcu file, not the GPIO file
			analogSignals = self.compactQuery("//Pin[@Name='{}']/Signal[starts-with(@Name,'ADC')]".format(name))
			pinSignals.extend(analogSignals)

			name = name[:4].split('-')[0].split('/')[0].strip()

			gpio = {'port': name[1:2], 'id': name[2:]}
			gpios.append(gpio)

			afs = []

			for signal in [s.get('Name') for s in pinSignals]:
				raw_names = signal.split('_')
				if len(raw_names) < 2:
					continue
				instance = raw_names[0][-1]
				if not instance.isdigit():
					instance = ""

				name = raw_names[1].lower()
				mode = None
				if name in nameToMode and nameToMode[name] != 'io':
					mode = nameToMode[name]
				af_id = None
				if signal in altFunctions:
					af_id = altFunctions[signal]

				if signal.startswith('USART') or signal.startswith('UART'):
					af = {'peripheral' : 'Uart' + instance,
						  'name': name.capitalize()}
					if mode:
						af.update({'type': mode})
					if af_id:
						af.update({'id': af_id})
					afs.append(af)

					mapName = {'rx': 'miso', 'tx': 'mosi', 'ck': 'sck'}
					if signal.startswith('USART') and name in mapName:
						af = {'peripheral' : 'UartSpiMaster' + instance,
							  'name': mapName[name].capitalize()}
						if mode:
							af.update({'type': mode})
						if af_id:
							af.update({'id': af_id})
						afs.append(af)

				elif signal.startswith('SPI'):
					af = {'peripheral' : 'SpiMaster' + instance,
						  'name': name.capitalize()}
					if mode:
						af.update({'type': mode})
					if af_id:
						af.update({'id': af_id})
					afs.append(dict(af))
					invertName = {'miso': 'somi', 'mosi': 'simo', 'nss': 'nss', 'sck': 'sck'}
					af.update({	'peripheral' : 'SpiSlave' + instance,
								'name': invertName[name].capitalize()})
					if mode:
						af.update({'type': invertMode[nameToMode[name]]})
					afs.append(af)

				if signal.startswith('CAN'):
					af = {'peripheral' : 'Can' + instance,
						  'name': name.capitalize()}
					if mode:
						af.update({'type': mode})
					if af_id:
						af.update({'id': af_id})
					afs.append(af)

				if signal.startswith('I2C'):
					if name in ['scl', 'sda']:
						af = {'peripheral' : 'I2cMaster' + instance,
							  'name': name.capitalize()}
						if mode:
							af.update({'type': mode})
						if af_id:
							af.update({'id': af_id})
						afs.append(af)

				if signal.startswith('TIM'):
					for tname in raw_names[1:]:
						tinstance = raw_names[0].replace('TIM', '')
						nice_name = 'ExternalTrigger'
						output_type = 'in'
						if 'CH' in tname:
							nice_name = tname.replace('CH', 'Channel')
							output_type = None
						elif 'BKIN' in tname:
							nice_name = 'BreakIn'
						af = {'peripheral' : 'Timer' + tinstance,
							  'name': nice_name}
						if output_type:
							af.update({'type': output_type})
						if af_id:
							af.update({'id': af_id})
						afs.append(af)

				if signal.startswith('ADC'):
					if 'exti' not in name:
						af = {'peripheral' : 'Adc' + instance,
							  'name': name.replace('in', 'Channel').capitalize(),
							  'type': 'analog'}
						afs.append(af)

				if signal.startswith('SYS'):
					if 'mco' in name:
						af = {'peripheral' : signal.replace('SYS', '').replace('_', ''),
							  'type': 'out',
							  'id': '0'}
						afs.append(af)

				if signal.startswith('OTG_FS') and raw_names[2] in ['DM', 'DP']:
					af = {'peripheral' : 'Usb',
						  'name': raw_names[2].capitalize()}
					if af_id:
						af.update({'id': af_id})
					else:
						af.update({'id': '10'})
					afs.append(af)

				# if signal.startswith('USB'):
				# 	af = {'peripheral' : 'Usb',
				# 		  'name': name.capitalize()}
				# 	if mode:
				# 		af.update({'type': mode})
				# 	if af_id:
				# 		af.update({'id': af_id})
				# 	afs.append(af)

				if signal.startswith('FSMC_'):
					if not raw_names[1].startswith('DA'):
						af = {'peripheral' : 'Fsmc',
							  'name': raw_names[1].capitalize()}
						if af_id:
							af.update({'id': af_id})
						afs.append(af)

			# sort after key id and then add all without ids
			# this sorting only affect the way the debug information is displayed
			# in stm_writer the AFs are sorted again anyway
			sorted_afs = [a for a in afs if 'id' in a]
			sorted_afs.sort(key=lambda k: (int(k['id'].split(',')[0]), k['peripheral']))
			sorted_afs.extend([a for a in afs if 'id' not in a])

			for af in sorted_afs:
				af['gpio_port'] = gpio['port']
				af['gpio_id'] = gpio['id']
				gpio_afs.append(af)

	def _modulesToString(self):
		string = ""
		char = self.modules[0][0:1]
		for module in self.modules:
			if not module.startswith(char):
				string += "\n"
			string += module + " \t"
			char = module[0][0:1]
		return string

	def _getDeviceDefine(self):
		if self.id.family not in stm32_defines:
			return None
		# get the defines for this device family
		familyDefines = stm32_defines[self.id.family]
		# get all defines for this device name
		devName = 'STM32F{}'.format(self.id.name)
		deviceDefines = sorted([define for define in familyDefines if define.startswith(devName)])
		# if there is only one define thats the one
		if len(deviceDefines) == 1:
			return deviceDefines[0]

		# now we match for the size-id.
		devName += 'x{}'.format(self.id.size_id.upper())
		for define in deviceDefines:
			if devName <= define:
				return define

		return None

	def __repr__(self):
		return self.__str__()

	def __str__(self):
		return "STMDeviceReader({}, [\n{}])".format(os.path.basename(self.name), ",\n".join(map(str, self.properties)))
