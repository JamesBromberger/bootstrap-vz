from base import Task
from common import phases
from common.tasks import workspace
from common.tasks import apt
import os
import shutil

assets = os.path.normpath(os.path.join(os.path.dirname(__file__), 'assets'))


class CreateVagrantBoxDir(Task):
	description = 'Creating directory for the vagrant box'
	phase = phases.preparation
	predecessors = [workspace.CreateWorkspace]

	@classmethod
	def run(cls, info):
		info.vagrant_folder = os.path.join(info.workspace, 'vagrant')
		os.mkdir(info.vagrant_folder)


class AddPackages(Task):
	description = 'Add packages that vagrant depends on'
	phase = phases.preparation
	predecessors = [apt.AddDefaultSources]

	@classmethod
	def run(cls, info):
		info.packages.add('openssh-server')
		info.packages.add('sudo')
		info.packages.add('nfs-client')


class CreateVagrantUser(Task):
	description = 'Creating the vagrant user'
	phase = phases.system_modification

	@classmethod
	def run(cls, info):
		from common.tools import log_check_call
		log_check_call(['/usr/sbin/chroot', info.root,
		                '/usr/sbin/useradd',
		                '--create-home', '--shell', '/bin/bash',
		                'vagrant'])


class PasswordlessSudo(Task):
	description = 'Allowing the vagrant user to use sudo without a password'
	phase = phases.system_modification

	@classmethod
	def run(cls, info):
		sudo_vagrant_path = os.path.join(info.root, 'etc/sudoers.d/vagrant')
		with open(sudo_vagrant_path, 'w') as sudo_vagrant:
			sudo_vagrant.write('vagrant ALL=(ALL) NOPASSWD:ALL')
		import stat
		ug_read_only = (stat.S_IRUSR | stat.S_IRGRP)
		os.chmod(sudo_vagrant_path, ug_read_only)


class AddInsecurePublicKey(Task):
	description = 'Adding vagrant insecure public key'
	phase = phases.system_modification
	predecessors = [CreateVagrantUser]

	@classmethod
	def run(cls, info):
		ssh_dir = os.path.join(info.root, 'home/vagrant/.ssh')
		os.mkdir(ssh_dir)

		authorized_keys_source_path = os.path.join(assets, 'authorized_keys')
		with open(authorized_keys_source_path, 'r') as authorized_keys_source:
			insecure_public_key = authorized_keys_source.read()

		authorized_keys_path = os.path.join(ssh_dir, 'authorized_keys')
		with open(authorized_keys_path, 'a') as authorized_keys:
			authorized_keys.write(insecure_public_key)


class SetRootPassword(Task):
	description = 'Setting the root password to `vagrant\''
	phase = phases.system_modification

	@classmethod
	def run(cls, info):
		from common.tools import log_check_call
		log_check_call(['/usr/sbin/chroot', info.root, '/usr/sbin/chpasswd'], 'root:vagrant')


class PackageBox(Task):
	description = 'Packaging the volume as a vagrant box'
	phase = phases.image_registration

	@classmethod
	def run(cls, info):
		box_basename = info.manifest.image['name'].format(**info.manifest_vars)
		box_name = '{name}.box'.format(name=box_basename)
		box_path = os.path.join(info.manifest.bootstrapper['workspace'], box_name)

		vagrantfile_source = os.path.join(assets, 'Vagrantfile')
		vagrantfile = os.path.join(info.vagrant_folder, 'Vagrantfile')
		shutil.copy(vagrantfile_source, vagrantfile)

		import random
		mac_address = '080027{mac:06X}'.format(mac=random.randrange(16 ** 6))
		from common.tools import sed_i
		sed_i(vagrantfile, '\\[MAC_ADDRESS\\]', mac_address)

		metadata_source = os.path.join(assets, 'metadata.json')
		metadata = os.path.join(info.vagrant_folder, 'metadata.json')
		shutil.copy(metadata_source, metadata)

		from common.tools import log_check_call
		disk_name = 'box-disk1.{ext}'.format(ext=info.volume.extension)
		disk_link = os.path.join(info.vagrant_folder, disk_name)
		log_check_call(['ln', '-s', info.volume.image_path, disk_link])

		ovf_path = os.path.join(info.vagrant_folder, 'box.ovf')
		cls.write_ovf(info, ovf_path, box_name, mac_address, disk_name)

		box_files = os.listdir(info.vagrant_folder)
		log_check_call(['tar', '--create', '--gzip', '--dereference',
		                '--file', box_path,
		                '--directory', info.vagrant_folder]
		               + box_files
		               )
		import logging
		logging.getLogger(__name__).info('The vagrant box has been placed at {box_path}'
		                                 .format(box_path=box_path))

	@classmethod
	def write_ovf(cls, info, destination, box_name, mac_address, disk_name):
		namespaces = {'ovf':     'http://schemas.dmtf.org/ovf/envelope/1',
		              'rasd':    'http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData',
		              'vssd':    'http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData',
		              'xsi':     'http://www.w3.org/2001/XMLSchema-instance',
		              'vbox':    'http://www.virtualbox.org/ovf/machine',
		              }

		def attr(element, name, value=None):
			for prefix, ns in namespaces.iteritems():
				name = name.replace(prefix + ':', '{' + ns + '}')
			if value is None:
				return element.attrib[name]
			else:
				element.attrib[name] = str(value)

		template_path = os.path.join(assets, 'box.ovf')
		import xml.etree.ElementTree as ET
		template = ET.parse(template_path)
		root = template.getroot()

		[disk_ref] = root.findall('./ovf:References/ovf:File', namespaces)
		attr(disk_ref, 'ovf:href', disk_name)

		# List of OVF disk format URIs
		# Snatched from VBox source (src/VBox/Main/src-server/ApplianceImpl.cpp:47)
		# ISOURI = "http://www.ecma-international.org/publications/standards/Ecma-119.htm"
		# VMDKStreamURI = "http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"
		# VMDKSparseURI = "http://www.vmware.com/specifications/vmdk.html#sparse"
		# VMDKCompressedURI = "http://www.vmware.com/specifications/vmdk.html#compressed"
		# VMDKCompressedURI2 = "http://www.vmware.com/interfaces/specifications/vmdk.html#compressed"
		# VHDURI = "http://go.microsoft.com/fwlink/?LinkId=137171"
		volume_uuid = info.volume.get_uuid()
		[disk] = root.findall('./ovf:DiskSection/ovf:Disk', namespaces)
		attr(disk, 'ovf:capacity', info.volume.partition_map.get_total_size() * 1024 * 1024)
		attr(disk, 'ovf:format', info.volume.ovf_uri)
		attr(disk, 'ovf:uuid', volume_uuid)

		[system] = root.findall('./ovf:VirtualSystem', namespaces)
		attr(system, 'ovf:id', box_name)

		[sysid] = system.findall('./ovf:VirtualHardwareSection/ovf:System/'
		                         'vssd:VirtualSystemIdentifier', namespaces)
		sysid.text = box_name

		[machine] = system.findall('./vbox:Machine', namespaces)
		import uuid
		attr(machine, 'ovf:uuid', uuid.uuid4())
		attr(machine, 'ovf:name', box_name)
		from datetime import datetime
		attr(machine, 'ovf:lastStateChange', datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'))
		[nic] = machine.findall('./ovf:Hardware/ovf:Network/ovf:Adapter', namespaces)
		attr(machine, 'ovf:MACAddress', mac_address)

		[device_img] = machine.findall('./ovf:StorageControllers'
		                               '/ovf:StorageController[@name="SATA Controller"]'
		                               '/ovf:AttachedDevice/ovf:Image', namespaces)
		attr(device_img, 'ovf:uuid', '{' + str(volume_uuid) + '}')

		template.write(destination, xml_declaration=True)  # , default_namespace=namespaces['ovf']


class RemoveVagrantBoxDir(Task):
	description = 'Removing the vagrant box directory'
	phase = phases.cleaning
	successors = [workspace.DeleteWorkspace]

	@classmethod
	def run(cls, info):
		shutil.rmtree(info.vagrant_folder)
		del info.vagrant_folder
