%global release_name juno
%global full_release heat-%{version}

%global with_doc %{!?_without_doc:1}%{?_without_doc:0}

Name:		openstack-heat
Summary:	OpenStack Orchestration (heat)
Version:	XXX
Release:	XXX{?dist}
License:	ASL 2.0
Group:		System Environment/Base
URL:		http://www.openstack.org
Source0:	http://tarballs.openstack.org/heat/%{full_release}.tar.gz
Obsoletes:	heat < 7-9
Provides:	heat

Source1:	heat.logrotate
%if 0%{?rhel} && 0%{?rhel} <= 6
Source2:	openstack-heat-api.init
Source3:	openstack-heat-api-cfn.init
Source4:	openstack-heat-engine.init
Source5:	openstack-heat-api-cloudwatch.init
%else
Source2:	openstack-heat-api.service
Source3:	openstack-heat-api-cfn.service
Source4:	openstack-heat-engine.service
Source5:	openstack-heat-api-cloudwatch.service
%endif
Source20:	heat-dist.conf

#
# patches_base=+0
#
Patch0001: 0001-remove-pbr-runtime-dependency.patch

BuildArch: noarch
BuildRequires: git
BuildRequires: python2-devel
BuildRequires: python-stevedore
BuildRequires: python-oslo-context
BuildRequires: python-oslo-middleware
BuildRequires: python-oslo-messaging
BuildRequires: python-setuptools
BuildRequires: python-oslo-sphinx
BuildRequires: python-oslo-i18n
BuildRequires: python-oslo-db
BuildRequires: python-oslo-utils
BuildRequires: python-argparse
BuildRequires: python-eventlet
BuildRequires: python-greenlet
BuildRequires: python-httplib2
BuildRequires: python-iso8601
BuildRequires: python-kombu
BuildRequires: python-lxml
BuildRequires: python-netaddr
BuildRequires: python-memcached
BuildRequires: python-migrate
BuildRequires: python-osprofiler
BuildRequires: python-qpid
BuildRequires: python-six
BuildRequires: PyYAML
BuildRequires: python-sphinx
BuildRequires: m2crypto
BuildRequires: python-paramiko
# These are required to build due to the requirements check added
BuildRequires: python-paste-deploy
BuildRequires: python-routes
BuildRequires: python-sqlalchemy
BuildRequires: python-webob
BuildRequires: python-pbr
BuildRequires: python-d2to1

%if ! (0%{?rhel} && 0%{?rhel} <= 6)
BuildRequires: systemd-units
%endif

%if 0%{?with_doc}
BuildRequires: python-oslo-config
BuildRequires: python-cinderclient
BuildRequires: python-keystoneclient
BuildRequires: python-novaclient
BuildRequires: python-saharaclient
BuildRequires: python-neutronclient
BuildRequires: python-swiftclient
BuildRequires: python-heatclient
BuildRequires: python-ceilometerclient
BuildRequires: python-glanceclient
BuildRequires: python-troveclient
%endif

Requires: %{name}-common = %{version}-%{release}
Requires: %{name}-engine = %{version}-%{release}
Requires: %{name}-api = %{version}-%{release}
Requires: %{name}-api-cfn = %{version}-%{release}
Requires: %{name}-api-cloudwatch = %{version}-%{release}

%prep
%setup -q -n heat-%{upstream_version}

%patch0001 -p1

sed -i s/REDHATHEATVERSION/%{version}/ heat/version.py
sed -i s/REDHATHEATRELEASE/%{release}/ heat/version.py

# Remove the requirements file so that pbr hooks don't add it
# to distutils requires_dist config
rm -rf {test-,}requirements.txt tools/{pip,test}-requires

# Remove tests in contrib
find contrib -name tests -type d | xargs rm -r

# Generate sample config
#tools/config/generate_sample.sh -b . -p heat -o etc/heat

# Programmatically update defaults in sample config
# which is installed at /etc/heat/heat.conf

#  First we ensure all values are commented in appropriate format.
#  Since icehouse, there was an uncommented keystone_authtoken section
#  at the end of the file which mimics but also conflicted with our
#  distro editing that had been done for many releases.
#sed -i '/^[^#[]/{s/^/#/; s/ //g}; /^#[^ ]/s/ = /=/' etc/heat/heat.conf.sample
#sed -i -e "s/^#heat_revision=.*$/heat_revision=%{version}-%{release}/I" etc/heat/heat.conf.sample

#  TODO: Make this more robust
#  Note it only edits the first occurance, so assumes a section ordering in sample
#  and also doesn't support multi-valued variables.
#while read name eq value; do
#  test "$name" && test "$value" || continue
#  sed -i "0,/^# *$name=/{s!^# *$name=.*!#$name=$value!}" etc/heat/heat.conf.sample
#done < %{SOURCE20}

%build
%{__python} setup.py build

%install
%{__python} setup.py install -O1 --skip-build --root=%{buildroot}
sed -i -e '/^#!/,1 d' %{buildroot}/%{python_sitelib}/heat/db/sqlalchemy/migrate_repo/manage.py
mkdir -p %{buildroot}/var/log/heat/
mkdir -p %{buildroot}/var/run/heat/
install -p -D -m 644 %{SOURCE1} %{buildroot}%{_sysconfdir}/logrotate.d/openstack-heat

%if 0%{?rhel} && 0%{?rhel} <= 6
# install init scripts
install -p -D -m 755 %{SOURCE2} %{buildroot}%{_initrddir}/openstack-heat-api
install -p -D -m 755 %{SOURCE3} %{buildroot}%{_initrddir}/openstack-heat-api-cfn
install -p -D -m 755 %{SOURCE4} %{buildroot}%{_initrddir}/openstack-heat-engine
install -p -D -m 755 %{SOURCE5} %{buildroot}%{_initrddir}/openstack-heat-api-cloudwatch
%else
# install systemd unit files
install -p -D -m 644 %{SOURCE2} %{buildroot}%{_unitdir}/openstack-heat-api.service
install -p -D -m 644 %{SOURCE3} %{buildroot}%{_unitdir}/openstack-heat-api-cfn.service
install -p -D -m 644 %{SOURCE4} %{buildroot}%{_unitdir}/openstack-heat-engine.service
install -p -D -m 644 %{SOURCE5} %{buildroot}%{_unitdir}/openstack-heat-api-cloudwatch.service
%endif

mkdir -p %{buildroot}/var/lib/heat/
mkdir -p %{buildroot}/etc/heat/

%if 0%{?with_doc}
export PYTHONPATH="$( pwd ):$PYTHONPATH"
pushd doc
sphinx-build -b html -d build/doctrees source build/html
sphinx-build -b man -d build/doctrees source build/man

mkdir -p %{buildroot}%{_mandir}/man1
install -p -D -m 644 build/man/*.1 %{buildroot}%{_mandir}/man1/
popd
%endif

rm -f %{buildroot}/%{_bindir}/heat-db-setup
rm -f %{buildroot}/%{_mandir}/man1/heat-db-setup.*
rm -rf %{buildroot}/var/lib/heat/.dummy
rm -f %{buildroot}/usr/bin/cinder-keystone-setup
rm -rf %{buildroot}/%{python_sitelib}/heat/tests

#install -p -D -m 640 etc/heat/heat.conf.sample %{buildroot}/%{_sysconfdir}/heat/heat.conf
install -p -D -m 640 %{SOURCE20} %{buildroot}%{_datadir}/heat/heat-dist.conf
install -p -D -m 640 etc/heat/api-paste.ini %{buildroot}/%{_datadir}/heat/api-paste-dist.ini
install -p -D -m 640 etc/heat/policy.json %{buildroot}/%{_sysconfdir}/heat

# TODO: move this to setup.cfg
cp -vr etc/heat/templates %{buildroot}/%{_sysconfdir}/heat
cp -vr etc/heat/environment.d %{buildroot}/%{_sysconfdir}/heat

%description
Heat provides AWS CloudFormation and CloudWatch functionality for OpenStack.


%package common
Summary: Heat common
Group: System Environment/Base

Requires: python-argparse
Requires: python-eventlet
Requires: python-stevedore >= 0.14
Requires: python-greenlet
Requires: python-httplib2
Requires: python-iso8601
Requires: python-kombu
Requires: python-lxml
Requires: python-netaddr
Requires: python-osprofiler
Requires: python-paste-deploy
Requires: python-posix_ipc
Requires: python-memcached
Requires: python-requests
Requires: python-routes
Requires: python-sqlalchemy
Requires: python-migrate
Requires: python-qpid
Requires: python-webob
Requires: python-six >= 1.4.1
Requires: PyYAML
Requires: python-anyjson
Requires: python-paramiko
Requires: python-babel
Requires: MySQL-python

Requires: python-oslo-config >= 1:1.2.0
Requires: python-oslo-context
Requires: python-oslo-utils
Requires: python-oslo-db
Requires: python-oslo-i18n
Requires: python-oslo-middleware
Requires: python-oslo-messaging
Requires: python-oslo-serialization

Requires: python-ceilometerclient
Requires: python-cinderclient
Requires: python-glanceclient
Requires: python-heatclient
Requires: python-keystoneclient
Requires: python-keystonemiddleware
Requires: python-neutronclient
Requires: python-novaclient
Requires: python-saharaclient
Requires: python-swiftclient
Requires: python-troveclient

Requires(pre): shadow-utils

%description common
Components common to all OpenStack Heat services

%files common
%doc LICENSE
%{_bindir}/heat-manage
%{_bindir}/heat-keystone-setup
%{_bindir}/heat-keystone-setup-domain
%{python_sitelib}/heat*
%attr(-, root, heat) %{_datadir}/heat/heat-dist.conf
%attr(-, root, heat) %{_datadir}/heat/api-paste-dist.ini
%dir %attr(0755,heat,root) %{_localstatedir}/log/heat
%dir %attr(0755,heat,root) %{_localstatedir}/run/heat
%dir %attr(0755,heat,root) %{_sharedstatedir}/heat
%dir %attr(0755,heat,root) %{_sysconfdir}/heat
%config(noreplace) %{_sysconfdir}/logrotate.d/openstack-heat
#%config(noreplace) %attr(-, root, heat) %{_sysconfdir}/heat/heat.conf
%config(noreplace) %attr(-, root, heat) %{_sysconfdir}/heat/policy.json
%config(noreplace) %attr(-,root,heat) %{_sysconfdir}/heat/environment.d/*
%config(noreplace) %attr(-,root,heat) %{_sysconfdir}/heat/templates/*
%if 0%{?with_doc}
%{_mandir}/man1/heat-keystone-setup.1.gz
%{_mandir}/man1/heat-keystone-setup-domain.1.gz
%{_mandir}/man1/heat-manage.1.gz
%endif

%pre common
# 187:187 for heat - rhbz#845078
getent group heat >/dev/null || groupadd -r --gid 187 heat
getent passwd heat  >/dev/null || \
useradd --uid 187 -r -g heat -d %{_sharedstatedir}/heat -s /sbin/nologin \
-c "OpenStack Heat Daemons" heat
exit 0

%package engine
Summary: The Heat engine
Group: System Environment/Base

Requires: %{name}-common = %{version}-%{release}

%if 0%{?rhel} && 0%{?rhel} <= 6
Requires(post): chkconfig
Requires(preun): chkconfig
Requires(preun): initscripts
Requires(postun): initscripts
%else
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd
%endif

%description engine
OpenStack API for starting CloudFormation templates on OpenStack

%files engine
%doc README.rst LICENSE
%if 0%{?with_doc}
%doc doc/build/html/man/heat-engine.html
%endif
%{_bindir}/heat-engine
%if 0%{?rhel} && 0%{?rhel} <= 6
%{_initrddir}/openstack-heat-engine
%else
%{_unitdir}/openstack-heat-engine.service
%endif
%if 0%{?with_doc}
%{_mandir}/man1/heat-engine.1.gz
%endif

%post engine
%if 0%{?rhel} && 0%{?rhel} <= 6
/sbin/chkconfig --add openstack-heat-engine
%else
%systemd_post openstack-heat-engine.service
%endif

%preun engine
%if 0%{?rhel} && 0%{?rhel} <= 6
if [ $1 -eq 0 ]; then
    /sbin/service openstack-heat-engine stop >/dev/null 2>&1
    /sbin/chkconfig --del openstack-heat-engine
fi
%else
%systemd_preun openstack-heat-engine.service
%endif

%postun engine
%if 0%{?rhel} && 0%{?rhel} <= 6
if [ $1 -ge 1 ]; then
    /sbin/service openstack-heat-engine condrestart >/dev/null 2>&1 || :
fi
%else
%systemd_postun_with_restart openstack-heat-engine.service
%endif


%package api
Summary: The Heat API
Group: System Environment/Base

Requires: %{name}-common = %{version}-%{release}

%if 0%{?rhel} && 0%{?rhel} <= 6
Requires(post): chkconfig
Requires(preun): chkconfig
Requires(preun): initscripts
Requires(postun): initscripts
%else
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd
%endif

%description api
OpenStack-native ReST API to the Heat Engine

%files api
%doc README.rst LICENSE
%if 0%{?with_doc}
%doc doc/build/html/man/heat-api.html
%endif
%{_bindir}/heat-api
%if 0%{?rhel} && 0%{?rhel} <= 6
%{_initrddir}/openstack-heat-api
%else
%{_unitdir}/openstack-heat-api.service
%endif
%if 0%{?with_doc}
%{_mandir}/man1/heat-api.1.gz
%endif

%post api
%if 0%{?rhel} && 0%{?rhel} <= 6
/sbin/chkconfig --add openstack-heat-api
%else
%systemd_post openstack-heat-api.service
%endif

%preun api
%if 0%{?rhel} && 0%{?rhel} <= 6
if [ $1 -eq 0 ]; then
    /sbin/service openstack-heat-api stop >/dev/null 2>&1
    /sbin/chkconfig --del openstack-heat-api
fi
%else
%systemd_preun openstack-heat-api.service
%endif

%postun api
%if 0%{?rhel} && 0%{?rhel} <= 6
if [ $1 -ge 1 ]; then
    /sbin/service openstack-heat-api condrestart >/dev/null 2>&1 || :
fi
%else
%systemd_postun_with_restart openstack-heat-api.service
%endif


%package api-cfn
Summary: Heat CloudFormation API
Group: System Environment/Base

Requires: %{name}-common = %{version}-%{release}

%if 0%{?rhel} && 0%{?rhel} <= 6
Requires(post): chkconfig
Requires(preun): chkconfig
Requires(preun): initscripts
Requires(postun): initscripts
%else
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd
%endif

%description api-cfn
AWS CloudFormation-compatible API to the Heat Engine

%files api-cfn
%doc README.rst LICENSE
%if 0%{?with_doc}
%doc doc/build/html/man/heat-api-cfn.html
%endif
%{_bindir}/heat-api-cfn
%if 0%{?rhel} && 0%{?rhel} <= 6
%{_initrddir}/openstack-heat-api-cfn
%else
%{_unitdir}/openstack-heat-api-cfn.service
%endif
%if 0%{?with_doc}
%{_mandir}/man1/heat-api-cfn.1.gz
%endif

%post api-cfn
%if 0%{?rhel} && 0%{?rhel} <= 6
/sbin/chkconfig --add openstack-heat-api-cfn
%else
%systemd_post openstack-heat-api-cloudwatch.service
%endif

%preun api-cfn
%if 0%{?rhel} && 0%{?rhel} <= 6
if [ $1 -eq 0 ]; then
    /sbin/service openstack-heat-api-cfn stop >/dev/null 2>&1
    /sbin/chkconfig --del openstack-heat-api-cfn
fi
%else
%systemd_preun openstack-heat-api-cloudwatch.service
%endif

%postun api-cfn
%if 0%{?rhel} && 0%{?rhel} <= 6
if [ $1 -ge 1 ]; then
    /sbin/service openstack-heat-api-cfn condrestart >/dev/null 2>&1 || :
fi
%else
%systemd_postun_with_restart openstack-heat-api-cloudwatch.service
%endif


%package api-cloudwatch
Summary: Heat CloudWatch API
Group: System Environment/Base

Requires: %{name}-common = %{version}-%{release}

%if 0%{?rhel} && 0%{?rhel} <= 6
Requires(post): chkconfig
Requires(preun): chkconfig
Requires(preun): initscripts
Requires(postun): initscripts
%else
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd
%endif

%description api-cloudwatch
AWS CloudWatch-compatible API to the Heat Engine

%files api-cloudwatch
%doc README.rst LICENSE
%if 0%{?with_doc}
%doc doc/build/html/man/heat-api-cloudwatch.html
%endif
%{_bindir}/heat-api-cloudwatch
%if 0%{?rhel} && 0%{?rhel} <= 6
%{_initrddir}/openstack-heat-api-cloudwatch
%else
%{_unitdir}/openstack-heat-api-cloudwatch.service
%endif
%if 0%{?with_doc}
%{_mandir}/man1/heat-api-cloudwatch.1.gz
%endif

%post api-cloudwatch
%if 0%{?rhel} && 0%{?rhel} <= 6
/sbin/chkconfig --add openstack-heat-api-cloudwatch
%else
%systemd_post openstack-heat-api-cfn.service
%endif

%preun api-cloudwatch
%if 0%{?rhel} && 0%{?rhel} <= 6
/sbin/chkconfig --add openstack-heat-api-cloudwatch
%else
%systemd_preun openstack-heat-api-cfn.service
%endif

%postun api-cloudwatch
%if 0%{?rhel} && 0%{?rhel} <= 6
if [ $1 -eq 0 ]; then
    /sbin/service openstack-heat-api-cloudwatch stop >/dev/null 2>&1
    /sbin/chkconfig --del openstack-heat-api-cloudwatch
fi

if [ $1 -ge 1 ]; then
    /sbin/service openstack-heat-api-cloudwatch condrestart >/dev/null 2>&1 || :
fi
%else
%systemd_postun_with_restart openstack-heat-api-cfn.service
%endif


%changelog
* Wed Dec 17 2014 Jeff Peeler <jpeeler@redhat.com> - XXX
- change systemd service files to not explicitly define logfile (rhbz#1083057)

* Tue Dec 16 2014 Jeff Peeler <jpeeler@redhat.com> - XXX
- Add heat sample config generation

* Mon Oct 27 2014 Dan Prince <dprince@redhat.com> - XXX
- Added python-oslo-middleware dependency.

* Fri Oct 17 2014 Ryan Brown <rybrown@redhat.com> - 2014.2-1
- Update to upstream 2014.2

* Mon Oct 13 2014 Ryan Brown <rybrown@redhat.com> - 2014.2-0.8.rc3
- Update to upstream 2014.2.rc3

* Mon Oct 13 2014 Ryan Brown <rybrown@redhat.com> - 2014.2-0.7.rc2
- Update to upstream 2014.2.rc2

* Fri Oct  3 2014 Ryan Brown <rybrown@redhat.com> - 2014.2-0.6.rc1
- Update to upstream 2014.2.rc1

* Tue Sep  9 2014 Ryan Brown <rybrown@redhat.com> - 2014.2-0.5.b3
- Add dependencies for oslo-i18n, keystonemiddleware, and saharaclient
- Update patches for 2014.2.b3

* Tue Sep  9 2014 Ryan Brown <rybrown@redhat.com> - 2014.2-0.1.b3
- Update to upstream 2014.2.b3

* Wed Aug 13 2014 Ryan Brown <rybrown@redhat.com> - 2014.2-0.4.b2
- Merge epel6 and fedora specfiles.

* Mon Aug  4 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.2-0.3.b2
- set qpid_topology_version=2 in heat-dist.conf (rhbz #1124137)
- add client requires (rhbz #1108056)
- remove m2crypto as it's no longer required

* Fri Jul 25 2014 Ryan S. Brown <rybrown@redhat.com> 2014.2-0.1.b2
- Update to upstream 2014.2.b2

* Tue Jul 15 2014 Ryan Brown <ryansb@redhat.com> - 2014.2-0.2.b1
- At build time add build+release to /etc/heat/heat.conf

* Fri Jun 20 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.2-0.1.b1
- updated to juno-1

* Fri Jun 13 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.1.1-2.1
- added heat-keystone-setup-domain script

* Tue Jun 10 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.1.1-2.0
- updated to 2014.1.1
- removed patch to build against python-oslo-sphinx and put change in spec

* Sat Jun 07 2014 Fedora Release Engineering <rel-eng@lists.fedoraproject.org> - 2014.1-2.0
- Rebuilt for https://fedoraproject.org/wiki/Fedora_21_Mass_Rebuild

* Tue Apr 22 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.1-1.0
- update to icehouse final

* Mon Apr 14 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.1-0.5.rc2
- update to icehouse-rc2

* Mon Apr  7 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.1-0.5.rc1
- update to icehouse-rc1

* Thu Mar  6 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.1-0.5.b3
- update to icehouse-3

* Tue Feb  4 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.1-0.5.b2
- fix heat-manage (rhbz 1060904)

* Mon Jan 27 2014 Jeff Peeler <jpeeler@redhat.com> - 2014.1-0.4.b2
- update to icehouse-2

* Mon Jan 06 2014 Pádraig Brady <pbrady@redhat.com> - 2014.1-0.4.b1
- Avoid [keystone_authtoken] config corruption in heat.conf

* Mon Jan 06 2014 Jeff Peeler <jpeeler@redhat.com> 2014-1.0.3.b1
- added MySQL-python requires
- removed heat-db-setup (rhbz 1046326)

* Mon Jan 06 2014 Pádraig Brady <pbrady@redhat.com> - 2014.1-0.2.b1
- Set python-six min version to ensure updated

* Mon Dec 09 2013 Jeff Peeler <jpeeler@redhat.com> 2014-1.0.1.b1
- update to icehouse-1
- add python-heatclient to BuildRequires

* Thu Oct 17 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-1
- update to havana final

* Mon Oct 14 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-0.9.rc2
- rebase to havana-rc2
- remove pbr dependency

* Thu Oct 3 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-0.9.rc1
- update to rc1
- exclude doc builds if with_doc 0

* Thu Sep 19 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-0.9.b3
- fix the python-oslo-config dependency to cater for epoch
- add api-paste-dist.ini to /usr/share/heat

*  Tue Sep 17 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-0.8.b3
- Depend on python-oslo-config >= 1.2 so it upgraded automatically
- Distribute dist defaults in heat-dist.conf separate to user heat.conf (rhbz 1008560)

* Wed Sep 11 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-0.7.b3
- fix init scripts (rhbz 1006868)
- added python-babel
- added python-pbr (rhbz 1006911)

* Mon Sep 9 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-0.6.b3
- rebase to havana-3
- remove tests from common
- remove cli package and move heat-manage into common
- added requires for python-heatclient
- remove python-boto as boto has been moved to another repo
- remove heat-cfn bash completion
- add /var/run/heat directory

* Sat Aug 03 2013 Fedora Release Engineering <rel-eng@lists.fedoraproject.org> - 2013.2-0.5.b2
- Rebuilt for https://fedoraproject.org/wiki/Fedora_20_Mass_Rebuild

* Tue Jul 30 2013 Pádraig Brady <pbrady@redhat.com> 2013.2-0.4.b2
- avoid python runtime dependency management

* Mon Jul 22 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-0.3.b2
- rebase to havana-2

* Mon Jun 10 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-0.2.b1
- fix m2crypto patch

* Tue Jun  4 2013 Jeff Peeler <jpeeler@redhat.com> 2013.2-0.1.b1
- rebase to havana-1
- consolidate api-paste files into one file in common
- removed runner.py as it is no longer present
- added heat-manage
- added new buildrequires pbr and d2to1

* Tue May 28 2013 Jeff Peeler <jpeeler@redhat.com> 2013.1-1.4
- bumped obsoletes for f18 rebuilds of the old heat package
- added missing policy.json file (rhbz#965549)
- changed require from python-paste to python-paste-deploy (rhbz#963207)

* Wed May  8 2013 Jeff Peeler <jpeeler@redhat.com> 2013.1-1.3
- removed python-crypto require

* Wed May  8 2013 Jeff Peeler <jpeeler@redhat.com> 2013.1-1.2
- re-added m2crypto patch (rhbz960165)

* Mon Apr 29 2013 Jeff Peeler <jpeeler@redhat.com> 2013.1-1.1
- modified engine script to not require full openstack install to start

* Mon Apr  8 2013 Jeff Peeler <jpeeler@redhat.com> 2013.1-1.0
- update to grizzly final

* Thu Mar 28 2013 Jeff Peeler <jpeeler@redhat.com> 2013.1-0.7.rc2
- bump to rc2

* Thu Mar 21 2013 Steven Dake <sdake@redhat.com> 2013.1-0.7.rc1
- Add all dependencies required
- Remove buildrequires of python-glanceclient

* Wed Mar 20 2013 Jeff Peeler <jpeeler@redhat.com> 2013.1-0.6.rc1
- Updated URL
- Added version for Obsoletes
- Removed dev suffix in builddir
- Added missing man pages

* Mon Mar 11 2013 Steven Dake <sdake@redhat.com> 2013.1-0.5.g3
- Assign heat user with 167:167
- Rename packages from *-api to api-*
- Rename clients to cli
- change user/gid to heat from openstack-heat
- use shared state dir macro for shared state
- Add /etc/heat dir to owned directory list
- set proper uid/gid for files
- set proper read/write/execute bits

* Thu Dec 20 2012 Jeff Peeler <jpeeler@redhat.com> 2013.1-2
- split into subpackages

* Fri Dec 14 2012 Steve Baker <sbaker@redhat.com> 2013.1-1
- rebase to 2013.1
- expunge heat-metadata
- generate man pages and html developer docs with sphinx

* Tue Oct 23 2012 Zane Bitter <zbitter@redhat.com> 7-1
- rebase to v7
- add heat-api daemon (OpenStack-native API)

* Fri Sep 21 2012 Jeff Peeler <jpeeler@redhat.com> 6-5
- update m2crypto patch (Fedora)
- fix user/group install permissions

* Tue Sep 18 2012 Steven Dake <sdake@redhat.com> 6-4
- update to new v6 binary names in heat

* Tue Aug 21 2012 Jeff Peeler <jpeeler@redhat.com> 6-3
- updated systemd scriptlets

* Tue Aug  7 2012 Jeff Peeler <jpeeler@redhat.com> 6-2
- change user/group ids to openstack-heat

* Wed Aug 1 2012 Jeff Peeler <jpeeler@redhat.com> 6-1
- create heat user and change file permissions
- set systemd scripts to run as heat user

* Fri Jul 27 2012 Ian Main <imain@redhat.com> - 5-1
- added m2crypto patch.
- bumped version for new release.
- added boto.cfg to sysconfigdir

* Tue Jul 24 2012 Jeff Peeler <jpeeler@redhat.com> - 4-5
- added LICENSE to docs
- added dist tag
- added heat directory to files section
- removed unnecessary defattr 

* Tue Jul 24 2012 Jeff Peeler <jpeeler@redhat.com> - 4-4
- remove pycrypto requires

* Fri Jul 20 2012 Jeff Peeler <jpeeler@redhat.com> - 4-3
- change python-devel to python2-devel

* Wed Jul 11 2012 Jeff Peeler <jpeeler@redhat.com> - 4-2
- add necessary requires
- removed shebang line for scripts not requiring executable permissions
- add logrotate, removes all rpmlint warnings except for python-httplib2
- remove buildroot tag since everything since F10 has a default buildroot
- remove clean section as it is not required as of F13
- add systemd unit files
- change source URL to download location which doesn't require a SHA

* Fri Jun 8 2012 Steven Dake <sdake@redhat.com> - 4-1
- removed jeos from packaging since that comes from another repository
- compressed all separate packages into one package
- removed setup options which were producing incorrect results
- replaced python with {__python}
- added a br on python-devel
- added a --skip-build to the install step
- added percent-dir for directories
- fixed most rpmlint warnings/errors

* Mon Apr 16 2012 Chris Alfonso <calfonso@redhat.com> - 3-1
- initial openstack package log
