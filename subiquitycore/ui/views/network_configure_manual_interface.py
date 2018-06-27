# Copyright 2015 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import ipaddress

from urwid import (
    connect_signal,
    Text,
    WidgetPlaceholder,
    )

from subiquitycore.view import BaseView
from subiquitycore.ui.buttons import menu_btn
from subiquitycore.ui.container import Pile
from subiquitycore.ui.form import (
    ChoiceField,
    Form,
    FormField,
    StringField,
    )
from subiquitycore.ui.interactive import RestrictedEditor, StringEditor
from subiquitycore.ui.selector import Option
from subiquitycore.ui.stretchy import Stretchy


log = logging.getLogger(
    'subiquitycore.network.network_configure_ipv4_interface')

ip_families = {
    4: {
        'address_cls': ipaddress.IPv4Address,
        'network_cls': ipaddress.IPv4Network,
    },
    6: {
        'address_cls': ipaddress.IPv6Address,
        'network_cls': ipaddress.IPv6Network,
    }
}


class IPField(FormField):
    def __init__(self, *args, **kw):
        self.has_mask = kw.pop('has_mask', False)
        super().__init__(*args, **kw)

    def _make_widget(self, form):
        if form.ip_version == 6:
            return StringEditor()
        else:
            if self.has_mask:
                allowed = '[0-9./]'
            else:
                allowed = '[0-9.]'
            return RestrictedEditor(allowed)


class NetworkConfigForm(Form):

    def __init__(self, ip_version, initial={}):
        self.ip_version = ip_version
        fam = ip_families[ip_version]
        self.ip_address_cls = fam['address_cls']
        self.ip_network_cls = fam['network_cls']
        super().__init__(initial)

    ok_label = _("Save")

    subnet = IPField(_("Subnet:"), has_mask=True)
    address = IPField(_("Address:"))
    gateway = IPField(_("Gateway:"))
    nameservers = StringField(_("Name servers:"),
                              help=_("IP addresses, comma separated"))
    searchdomains = StringField(_("Search domains:"),
                                help=_("Domains, comma separated"))

    def clean_subnet(self, subnet):
        log.debug("clean_subnet %r", subnet)
        if '/' not in subnet:
            raise ValueError(_("should be in CIDR form (xx.xx.xx.xx/yy)"))
        return self.ip_network_cls(subnet)

    def clean_address(self, address):
        address = self.ip_address_cls(address)
        try:
            subnet = self.subnet.value
        except ValueError:
            return
        if address not in subnet:
            raise ValueError(
                _("'%s' is not contained in '%s'") % (address, subnet))
        return address

    def clean_gateway(self, gateway):
        if not gateway:
            return None
        return self.ip_address_cls(gateway)

    def clean_nameservers(self, value):
        nameservers = []
        for ns in value.split(','):
            ns = ns.strip()
            if ns:
                nameservers.append(ipaddress.ip_address(ns))
        return nameservers

    def clean_searchdomains(self, value):
        domains = []
        for domain in value.split(','):
            domain = domain.strip()
            if domain:
                domains.append(domain)
        return domains


network_choices = [
    (_("Automatic (DHCP)"), True, "dhcp"),
    (_("Manual"), True, "manual"),
    (_("Disabled"), True, "disable"),
    ]


class NetworkMethodForm(Form):
    ok_label = _("Save")
    method = ChoiceField("IPv{ip_version} Method: ", choices=network_choices)


class EditNetworkStretchy(Stretchy):

    def __init__(self, parent, device, ip_version):
        self.parent = parent
        self.device = device
        self.ip_version = ip_version

        self.method_form = NetworkMethodForm()
        self.method_form.method.caption = _("IPv{ip_version} Method: ").format(ip_version=ip_version)
        manual_initial = {}
        if len(device.configured_ip_addresses_for_version(ip_version)) > 0:
            method = 'manual'
            addr = ipaddress.ip_interface(
                device.configured_ip_addresses_for_version(ip_version)[0])
            manual_initial = {
                'subnet': str(addr.network),
                'address': str(addr.ip),
                'nameservers': ', '.join(device.configured_nameservers),
                'searchdomains': ', '.join(device.configured_searchdomains),
            }
            gw = device.configured_gateway_for_version(ip_version)
            if gw:
                manual_initial['gateway'] = str(gw)
        elif self.device.dhcp_for_version(ip_version):
            method = 'dhcp'
        else:
            method = 'disable'

        self.method_form.method.value = method

        connect_signal(self.method_form.method.widget, 'select', self._select_method)

        log.debug("manual_initial %s", manual_initial)
        self.manual_form = NetworkConfigForm(ip_version, manual_initial)

        connect_signal(self.method_form, 'submit', self.done)
        connect_signal(self.manual_form, 'submit', self.done)
        connect_signal(self.method_form, 'cancel', self.cancel)
        connect_signal(self.manual_form, 'cancel', self.cancel)

        self.form_pile = Pile(self.method_form.as_rows())

        self.bp = WidgetPlaceholder(self.method_form.buttons)

        self._select_method(None, method)

        widgets = [self.form_pile, Text(""), self.bp]
        super().__init__(
            "Edit {device} IPv{ip_version} configuration".format(device=device.name, ip_version=ip_version),
            widgets,
            0, 0)

    def _select_method(self, sender, method):
        rows = []
        def r(w):
            rows.append((w, self.form_pile.options('pack')))
        for row in self.method_form.as_rows():
            r(row)
        if method == 'manual':
            r(Text(""))
            for row in self.manual_form.as_rows():
                r(row)
            self.bp.original_widget = self.manual_form.buttons
        else:
            self.bp.original_widget = self.method_form.buttons
        self.form_pile.contents[:] = rows

    def done(self, sender):

        self.device.remove_ip_networks_for_version(self.ip_version)
        self.device.set_dhcp_for_version(self.ip_version, False)

        if self.method_form.method.value == "manual":
            form = self.manual_form
            # XXX this converting from and to and from strings thing is a
            # bit out of hand.
            gateway = form.gateway.value
            if gateway is not None:
                gateway = str(gateway)
            result = {
                'network': str(form.subnet.value),
                'address': str(form.address.value),
                'gateway': gateway,
                'nameservers': list(map(str, form.nameservers.value)),
                'searchdomains': form.searchdomains.value,
            }
            self.device.remove_nameservers()
            self.device.add_network(self.ip_version, result)
        elif self.method_form.method.value == "dhcp":
            self.device.set_dhcp_for_version(self.ip_version, True)
        else:
            pass
        self.parent.refresh_model_inputs()
        self.parent.remove_overlay()

    def cancel(self, sender=None):
        self.parent.remove_overlay()


class BaseNetworkConfigureManualView(BaseView):

    def __init__(self, model, controller, name):
        self.model = model
        self.controller = controller
        self.dev = self.model.get_netdev_by_name(name)
        self.title = _("Network interface {} manual IPv{} "
                       "configuration").format(name, self.ip_version)
        self.is_gateway = False
        self.form = NetworkConfigForm(self.ip_version)
        connect_signal(self.form, 'submit', self.done)
        connect_signal(self.form, 'cancel', self.cancel)

        self.form.subnet.help = _("Example: %s" % (self.example_address,))
        configured_addresses = (
            self.dev.configured_ip_addresses_for_version(self.ip_version))
        if configured_addresses:
            addr = ipaddress.ip_interface(configured_addresses[0])
            self.form.subnet.value = str(addr.network)
            self.form.address.value = str(addr.ip)
        configured_gateway = (
            self.dev.configured_gateway_for_version(self.ip_version))
        if configured_gateway:
            self.form.gateway.value = configured_gateway
        self.form.nameservers.value = (
            ', '.join(self.dev.configured_nameservers))
        self.form.searchdomains.value = (
            ', '.join(self.dev.configured_searchdomains))
        self.error = Text("", align='center')

        super().__init__(self.form.as_screen(focus_buttons=False))

    def refresh_model_inputs(self):
        try:
            self.dev = self.model.get_netdev_by_name(self.dev.name)
        except KeyError:
            # The interface is gone
            self.controller.default()
            return

    def _build_set_as_default_gw_button(self):
        devs = self.model.get_all_netdevs()

        self.is_gateway = self.model.v4_gateway_dev == self.dev.name

        if not self.is_gateway and len(devs) > 1:
            btn = menu_btn(label=_("Set this as default gateway"),
                           on_press=self.set_default_gateway)
        else:
            btn = Text(_("This will be your default gateway"))

        return [btn]

    def set_default_gateway(self, button):
        if self.gateway_input.value:
            try:
                self.model.set_default_v4_gateway(self.dev.name,
                                                  self.gateway_input.value)
                self.is_gateway = True
                self.set_as_default_gw_button.contents = [
                    (obj, ('pack', None))
                    for obj in self._build_set_as_default_gw_button()]
            except ValueError:
                # FIXME: set error message UX ala identity
                pass

    def done(self, sender):
        # XXX this converting from and to and from strings thing is a
        # bit out of hand.
        gateway = self.form.gateway.value
        if gateway is not None:
            gateway = str(gateway)
        result = {
            'network': str(self.form.subnet.value),
            'address': str(self.form.address.value),
            'gateway': gateway,
            'nameservers': list(map(str, self.form.nameservers.value)),
            'searchdomains': self.form.searchdomains.value,
        }
        self.dev.remove_ip_networks_for_version(self.ip_version)
        self.dev.remove_nameservers()
        self.dev.add_network(self.ip_version, result)

        # return
        self.controller.network_configure_interface(self.dev.name)

    def cancel(self, sender=None):
        self.model.default_gateway = None
        self.controller.network_configure_interface(self.dev.name)


class NetworkConfigureIPv4InterfaceView(BaseNetworkConfigureManualView):
    ip_version = 4
    example_address = '192.168.9.0/24'


class NetworkConfigureIPv6InterfaceView(BaseNetworkConfigureManualView):
    ip_version = 6
    example_address = 'fde4:8dba:82e1::/64'
