Name:       python-pyvmwareapi
Version:    0.0.1
Release:    1
Summary:    Python module to use vmware API

Group:      Development/Languages
License:    ASL 2.0
BuildArch:  noarch

Source0:    pyvmwareapi-%{version}.tar.gz

Requires:   python-eventlet
Requires:   python-suds
Requires:   python-httplib2 >= 0.7

BuildRequires: python2-devel
BuildRequires: python-setuptools

%description
Python module to use vmware API

%prep
%setup -q -n pyvmwareapi-%{version}

%build
%{__python} setup.py build

%install
%{__python} setup.py install -O1 --skip-build --root %{buildroot}

%files
%{_bindir}/pyvmwareapi
%{python_sitelib}/pyvmwareapi
%{python_sitelib}/*.egg-info

%changelog
* Fri Aug 23 2013 Roman Sokolkov <rsokolkov@mirantis.com> - 0.0.1
- Initial package
