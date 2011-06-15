import os
import sys
from distutils.core import setup

path, script = os.path.split(sys.argv[0])
os.chdir(os.path.abspath(path))

setup(name='domaincli',
      version='0.0.5',
      description='Register domains from the command line',
      author='Greg Brockman',
      author_email='gdb@gregbrockman.com',
      url='https://github.com/thegdb/domaincli',
      scripts=['domaincli'],
      install_requires='stripe'
)
