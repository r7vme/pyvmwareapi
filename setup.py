#!/usr/bin/python
# vim: tabstop=4 shiftwidth=4 softtabstop=4

import os
import subprocess

from setuptools import setup, find_packages


setup(
    name='pyvmwareapi',
    version='0.0.1',
    description='Provide ability to create, destroy, reboot VMs under ESXi',
    license='Apache License (2.0)',
    author='Roman Sokolkov, Oleg Balakirev',
    author_email='rsokolkov@mirantis.com',
    packages=find_packages(exclude=['tests', 'bin']),
    test_suite='nose.collector',
    include_package_data=True,
    classifiers=[
        'Development Status :: 4 - Beta',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 2.6',
        'Environment :: No Input/Output (Daemon)',
    ],
    scripts=['bin/test.py'])
