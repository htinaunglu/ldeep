#!/usr/bin/env python3

from argparse import ArgumentParser
from json import dump as json_dump
from base64 import b64encode
from math import fabs
from re import compile as re_compile
from datetime import date, datetime, timedelta
from commandparse import Command
from time import sleep

from pyasn1.error import PyAsn1UnicodeDecodeError

from ldeep.views.activedirectory import ActiveDirectoryView, ALL, ALL_ATTRIBUTES, ALL_OPERATIONAL_ATTRIBUTES
from ldeep.views.constants import USER_ACCOUNT_CONTROL, LDAP_SERVER_SD_FLAGS_OID_SEC_DESC
from ldeep.views.ldap_activedirectory import LdapActiveDirectoryView
from ldeep.views.cache_activedirectory import CacheActiveDirectoryView

from ldeep.utils import error, info, Logger, resolve as utils_resolve
from ldeep.utils.sddl import parse_ntSecurityDescriptor
from ldap3.protocol.formatters.formatters import format_sid
from ldap3.core import results as coreResults

import sys


class Ldeep(Command):

    def __init__(self, query_engine, format="json"):
        self.engine = query_engine
        if format == "json":
            self.__display = self.__display_json

    def display(self, records, verbose=False, specify_group=True):
        def default(o):
            if isinstance(o, date) or isinstance(o, datetime):
                return o.isoformat()
            elif isinstance(o, bytes):
                return b64encode(o).decode('ascii')

        if verbose:
            # self.__display(list(map(dict, records)), default)
            self.__display(records, default)
        else:
            k = 0
            for record in records:
                k += 1
                if "objectClass" not in record:
                    print(record)
                elif "group" in record["objectClass"]:
                    print(record["sAMAccountName"] + (" (group)" if specify_group else ""))
                elif "user" in record["objectClass"]:
                    print(record["sAMAccountName"])
                elif "dnsNode" in record["objectClass"]:
                    print("{dc} {rec}".format(dc=record["dc"], rec=" ".join(record["dnsRecord"])))
                elif "dnsZone" in record["objectClass"]:
                    print(record["dc"])
                elif "domain" in record["objectClass"]:
                    print(record["dn"])
                elif "pKIEnrollmentService" in record["objectClass"]:
                    print(record["dNSHostName"])
                elif "msDS-AuthNPolicy" in record["objectClass"] or "msDS-AuthNPolicySilo" in record["objectClass"]:
                    print(record["cn"])

                if self.engine.page_size > 0 and k % self.engine.page_size == 0:
                    sleep(self.engine.throttle)

    def __display_json(self, records, default):
        need_comma_sep = False
        k = 0

        sys.stdout.write("[")
        for record in records:
            k += 1
            if need_comma_sep:
                sys.stdout.write(",\n")
            else:
                need_comma_sep = True

            json_dump(record, sys.stdout, ensure_ascii=False, default=default, sort_keys=True, indent=2)

            if self.engine.page_size > 0 and k % self.engine.page_size == 0:
                sleep(self.engine.throttle)

        sys.stdout.write("]\n")
        sys.stdout.flush()

    # LISTERS #

    def list_users(self, kwargs):
        """
        List users according to a filter.

        Arguments:
            @verbose:bool
                Results will contain full information
            @filter:string = ["all", "spn", "enabled", "disabled", "locked", "nopasswordexpire", "passwordexpired", "nokrbpreauth", "reversible"]
        """
        verbose = kwargs.get("verbose", False)
        filter_ = kwargs.get("filter", "all")

        if verbose:
            attributes = self.engine.all_attributes()
        else:
            attributes = ["sAMAccountName", "objectClass"]

        if filter_ == "all":
            results = self.engine.query(self.engine.USER_ALL_FILTER(), attributes)
        elif filter_ == "spn":
            results = self.engine.query(self.engine.USER_SPN_FILTER(), attributes)
        elif filter_ == "enabled":
            results = self.engine.query(self.engine.USER_ACCOUNT_CONTROL_FILTER_NEG(USER_ACCOUNT_CONTROL["ACCOUNTDISABLE"]), attributes)
        elif filter_ == "disabled":
            results = self.engine.query(self.engine.USER_ACCOUNT_CONTROL_FILTER(USER_ACCOUNT_CONTROL["ACCOUNTDISABLE"]), attributes)
        elif filter_ == "locked":
            results = self.engine.query(self.engine.USER_LOCKED_FILTER(), attributes)
        elif filter_ == "nopasswordexpire":
            results = self.engine.query(self.engine.USER_ACCOUNT_CONTROL_FILTER(USER_ACCOUNT_CONTROL["DONT_EXPIRE_PASSWORD"]), attributes)
        elif filter_ == "passwordexpired":
            results = self.engine.query(self.engine.USER_ACCOUNT_CONTROL_FILTER(USER_ACCOUNT_CONTROL["PASSWORD_EXPIRED"]), attributes)
        elif filter_ == "nokrbpreauth":
            results = self.engine.query(self.engine.USER_ACCOUNT_CONTROL_FILTER(USER_ACCOUNT_CONTROL["DONT_REQ_PREAUTH"]), attributes)
        elif filter_ == "reversible":
            results = self.engine.query(self.engine.USER_ACCOUNT_CONTROL_FILTER(USER_ACCOUNT_CONTROL["ENCRYPTED_TEXT_PWD_ALLOWED"]), attributes)
        else:
            return None

        self.display(results, verbose)

    def list_groups(self, kwargs):
        """
        List the groups.

        Arguments:
            @verbose:bool
                Results will contain full information
        """
        verbose = kwargs.get("verbose", False)

        if verbose:
            attributes = self.engine.all_attributes()
        else:
            attributes = ["sAMAccountName", "objectClass"]

        self.display(self.engine.query(self.engine.GROUPS_FILTER(), attributes), verbose, specify_group=False)

    def list_machines(self, kwargs):
        """
        List the machine accounts.

        Arguments:
            @verbose:bool
                Results will contain full information
        """
        verbose = kwargs.get("verbose", False)

        if verbose:
            attributes = self.engine.all_attributes()
        else:
            attributes = ["sAMAccountName", "objectClass"]

        self.display(self.engine.query(self.engine.COMPUTERS_FILTER(), attributes), verbose, specify_group=False)

    def list_computers(self, kwargs):
        """
        List the computer hostnames and resolve them if --resolve is specify.

        Arguments:
            @resolve:bool
                A resolution on all computer names will be performed
            @dns:string
                An optional DNS server to use for the resolution
            @dc:bool
                List only domain controllers
        """
        resolve = "resolve" in kwargs and kwargs["resolve"]
        dns = kwargs.get("dns", "")
        dc = kwargs.get("dc", False)

        hostnames = []
        if not dc:
            results = self.engine.query(self.engine.COMPUTERS_FILTER(), ["name"])
        else:
            results = self.engine.query(self.engine.DC_FILTER(), ["name"])
        for result in results:
            if "name" in result:  # ugly
                computer_name = result["name"]
            else:
                computer_name = result[:-1]  # removing trailing $ sign

            hostnames.append(f"{computer_name}.{self.engine.fqdn}")
            # print only if resolution was not mandated
            if not resolve:
                print(f"{computer_name}.{self.engine.fqdn}")
        # do the resolution
        if resolve:
            for computer in utils_resolve(hostnames, dns):
                print("{addr:20} {name}".format(addr=computer["address"], name=computer["hostname"]))

    def list_gmsa(self, kwargs):
        """
        List the gmsa accounts and retrieve NT hash if possible.

        Arguments:
            @verbose:bool
                Results will contain full information
        """
        verbose = kwargs.get("verbose", False)
        hidden_attributes = ["msDS-ManagedPassword"]
        if verbose:
            attributes = ALL + hidden_attributes
        else:
            attributes = ["sAMAccountName", "objectClass"] + hidden_attributes

        entries = self.engine.get_gmsa(attributes)
        if verbose:
            self.display(entries, verbose)
        else:
            first = True
            for entry in entries:
                if not first:
                    print()
                print(f"sam:{entry['sAMAccountName']}")
                if "nthash" in entry:
                    print(f"nthash:{entry['nthash']}")
                    print(f"aes128-cts-hmac-sha1-96:{entry['aes128-cts-hmac-sha1-96']}")
                    print(f"aes256-cts-hmac-sha1-96:{entry['aes256-cts-hmac-sha1-96']}")
                first = False

    def list_domain_policy(self, _):
        """
        Return the domain policy.
        """
        FILETIME_TIMESTAMP_FIELDS = {
            "lockOutObservationWindow": (60, "mins"),
            "lockoutDuration": (60, "mins"),
            "maxPwdAge": (86400, "days"),
            "minPwdAge": (86400, "days"),
            "forceLogoff": (60, "mins")
        }

        FOREST_LEVELS = {
            7: "Windows Server 2016",
            6: "Windows Server 2012 R2",
            5: "Windows Server 2012",
            4: "Windows Server 2008 R2",
            3: "Windows Server 2008",
            2: "Windows Server 2003",
            1: "Windows Server 2003 operating system through Windows Server 2016",
            0: "Windows 2000 Server operating system through Windows Server 2008 operating system"
        }

        FIELDS_TO_PRINT = [
            "dc",
            "distinguishedName",
            "lockOutObservationWindow",
            "lockoutDuration",
            "lockoutThreshold",
            "maxPwdAge",
            "minPwdAge",
            "minPwdLength",
            "pwdHistoryLength",
            "pwdProperties",
            "ms-DS-MachineAccountQuota",
            "msDS-Behavior-Version"]

        policy = list(self.engine.query(self.engine.DOMAIN_INFO_FILTER()))
        if policy:
            policy = policy[0]
            for field in FIELDS_TO_PRINT:
                val = policy.get(field, None)
                if val is None:
                    continue

                if field == "lockOutObservationWindow" and isinstance(val, timedelta):
                    val = int(val.total_seconds()) / 60
                elif field in FILETIME_TIMESTAMP_FIELDS.keys() and type(val) == int:
                    val = int((fabs(float(val)) / 10**7) / FILETIME_TIMESTAMP_FIELDS[field][0])
                if field in FILETIME_TIMESTAMP_FIELDS.keys():
                    val = "%d %s" % (val, FILETIME_TIMESTAMP_FIELDS[field][1])
                if field == "msDS-Behavior-Version" and isinstance(val, int):
                    val = "%s" % (FOREST_LEVELS[policy[field]])

                print("%s: %s" % (field, val))

    def list_ou(self, _):
        """
        Return the list of organizational units with linked GPO.
        """
        cn_re = re_compile("{[^}]+}")
        results = self.engine.query(self.engine.GPO_INFO_FILTER(), ["cn", "displayName"])
        gpos = {}
        for gpo in results:
            gpos[gpo["cn"]] = gpo["displayName"]

        results = self.engine.query(self.engine.OU_FILTER())
        for result in results:
            print(result["distinguishedName"])
            if "gPLink" in result:
                guids = cn_re.findall(result["gPLink"])
                if len(guids) > 0:
                    print("[gPLink]")
                    print("* {}".format("\n* ".join([gpos[g] if g in gpos else g for g in guids])))

    def list_gpo(self, _):
        """
        Return the list of Group policy objects.
        """
        results = self.engine.query(self.engine.GPO_INFO_FILTER(), ["cn", "displayName"])
        for gpo in results:
            print("{cn}: {name}".format(cn=gpo["cn"], name=gpo["displayName"]))

    def list_pso(self, _):
        """
        List the Password Settings Objects.
        """
        FILETIME_TIMESTAMP_FIELDS = {
            "msDS-LockoutObservationWindow": (60, "mins"),
            "msDS-MinimumPasswordAge": (86400, "days"),
            "msDS-MaximumPasswordAge": (86400, "days"),
            "msDS-LockoutDuration": (60, "mins")
        }

        FIELDS_TO_PRINT = [
            "cn",
            "msDS-PasswordReversibleEncryptionEnabled",
            "msDS-PasswordSettingsPrecedence",
            "msDS-MinimumPasswordLength",
            "msDS-PasswordHistoryLength",
            "msDS-PasswordComplexityEnabled",
            "msDS-LockoutObservationWindow",
            "msDS-LockoutDuration",
            "msDS-LockoutThreshold",
            "msDS-MinimumPasswordAge",
            "msDS-MaximumPasswordAge",
            "msDS-PSOAppliesTo",
        ]

        psos = self.engine.query(self.engine.PSO_INFO_FILTER())
        for policy in psos:
            for field in FIELDS_TO_PRINT:
                val = policy.get(field, None)
                if not val:
                    continue
                if isinstance(val, list):
                    targets = []
                    for target in val:
                        targets.append(target)
                    val = " | ".join(targets)
                else:
                    val = policy[field]

                if field in FILETIME_TIMESTAMP_FIELDS.keys():
                    val = int((fabs(float(val)) / 10**7) / FILETIME_TIMESTAMP_FIELDS[field][0])
                    val = "{val} {typ}".format(val=val, typ=FILETIME_TIMESTAMP_FIELDS[field][1])
                print("{field}: {val}".format(field=field, val=val))

    def list_trusts(self, kwargs):
        """
        List the domain's trust relationships.

        Arguments:
            @verbose:bool
                Results will contain full information
        """
        verbose = kwargs.get("verbose", False)
        results = self.engine.query(self.engine.TRUSTS_INFO_FILTER())

        ATTRIBUTE_TRANSLATION = {
            "trustDirection": {
                0x00000003: "bidirectional",
                0x00000002: "outbound",
                0x00000001: "inbound",
                0x00000000: "disabled",
            },
            "trustType": {
                0x00000001: "Non running Windows domain",
                0x00000002: "Windows domain running Active Directory",
                0x00000003: "Non Windows domain",
            },
        }

        trusts = []
        for result in results:
            for key in ATTRIBUTE_TRANSLATION:
                if key in result:
                    result[key] = ATTRIBUTE_TRANSLATION[key][int(result[key])]
            trusts.append(result)

        if verbose:
            self.display(results, verbose)
            return

        FIELDS_TO_PRINT = [
            "dn",
            "cn",
            "securityIdentifier",
            "name",
            "trustDirection",
            "trustPartner",
            "trustType",
            "trustAttributes",
            "flatName"
        ]

        for result in trusts:
            for field in FIELDS_TO_PRINT:
                if field in result:
                    val = result[field]
                    print("{field}: {val}".format(field=field, val=val))
            print("")

    def list_zones(self, kwargs):
        """
        List the DNS zones configured in the Active Directory.

        Arguments:
            @verbose:bool
                Results will contain full information
        """
        verbose = kwargs.get("verbose", False)

        if not verbose:
            attributes = ["dc", "objectClass"]
        else:
            attributes = ALL

        self.display(
            self.engine.query(
                self.engine.ZONES_FILTER(),
                attributes, base=','.join(["CN=MicrosoftDNS,DC=DomainDNSZones", self.engine.base_dn])
            ),
            verbose
        )

    def list_pkis(self, kwargs):
        """
        List pkis.
        Arguments:
            @verbose:bool
                Results will contain full information
        """
        verbose = kwargs.get("verbose", False)

        if verbose:
            attributes = self.engine.all_attributes()
        else:
            attributes = ["dNSHostName", "objectClass"]

        self.display(
            self.engine.query(
                self.engine.PKI_FILTER(),
                attributes,
                base=','.join(["CN=Enrollment Services,CN=Public Key Services,CN=Services,CN=CONFIGURATION", self.engine.base_dn]),
            ),
            verbose
        )

    # subnet_dn not used, TODO: remove?
    def list_subnets(self, kwargs):
        """
        List sites and associated subnets.
        Arguments:
            @verbose:bool
                Results will contain full information
        """
        verbose = kwargs.get("verbose", False)

        if not verbose:
            attributes = ["distinguishedName", "name", "description"]
        else:
            attributes = ALL

        if verbose:
            self.display(
                self.engine.query(
                    self.engine.SITES_FILTER(),
                    attributes, base=','.join(["CN=Configuration", self.engine.base_dn])
                ),
                verbose
            )
        else:
            entries = self.engine.query(self.engine.SITES_FILTER(), attributes, base=','.join(["CN=Configuration", self.engine.base_dn]))

            site_dn = ""
            site_name = ""
            site_description = ""
            # subnet_dn = ""
            subnet_name = ""
            subnet_description = ""
            for entry in entries:
                site_dn = entry["distinguishedName"] if entry["distinguishedName"] else ""
                site_name = entry["name"] if entry["name"] else ""
                site_description = entry["description"][0] if entry["description"] else ""
                subnet_entries = self.engine.query(self.engine.SUBNET_FILTER(site_dn), attributes, base=','.join(["CN=Sites,CN=Configuration", self.engine.base_dn]))
                for subnet in subnet_entries:
                    # subnet_dn = subnet["distinguishedName"] if subnet["distinguishedName"] else ""
                    subnet_name = subnet["name"] if subnet["name"] else ""
                    subnet_description = subnet["description"][0] if subnet["description"] else ""
                    servers = self.engine.query("(objectClass=server)", ['cn'], base=site_dn)
                    servers_list = [d['cn'] for d in servers]

                    output = "Site: {}".format(site_name)
                    output += " | Subnet: {}".format(subnet_name) if subnet_name else ""
                    output += " | Site description: {}".format(site_description) if site_description else ""
                    output += " | Subnet description: {}".format(subnet_description) if subnet_description else ""
                    output += " | Servers: {}".format(', '.join(servers_list)) if servers_list else ""
                    print(output)

    def list_conf(self, kwargs):
        """
        Dump the configuration partition of the Active Directory.
        """
        self.display(
            self.engine.query(
                self.engine.ALL_FILTER(),
                ALL, base=','.join(["CN=Configuration", self.engine.base_dn])
            ),
            True
        )

    def list_auth_policies(self, kwargs):
        """
        List the authentication policies configured in the Active Directory.

        Arguments:
            @verbose:bool
                Results will contain full information
        """
        verbose = kwargs.get("verbose", False)
        attributes = ALL if verbose else ["cn", "objectClass"]

        self.display(
            self.engine.query(
                self.engine.AUTH_POLICIES_FILTER(),
                attributes, base=','.join(["CN=AuthN Policy Configuration,CN=Services,CN=Configuration", self.engine.base_dn])
            ),
            verbose
        )

    def list_silos(self, kwargs):
        """
        List the silos configured in the Active Directory.

        Arguments:
            @verbose:bool
                Results will contain full information
        """
        verbose = kwargs.get("verbose", False)
        attributes = ALL if verbose else ["cn", "objectClass"]

        self.display(
            self.engine.query(
                self.engine.SILOS_FILTER(),
                attributes, base=','.join(["CN=AuthN Policy Configuration,CN=Services,CN=Configuration", self.engine.base_dn])
            ),
            verbose
        )

    def list_smsa(self, kwargs):
        """
        List the smsa accounts and the machines they are associated with.

        Arguments:
            @verbose:bool
                Results will contain full information
        """

        verbose = kwargs.get("verbose", False)
        attributes = ALL if verbose else ["sAMAccountName", "msDS-HostServiceAccountBL"]
        entries = self.engine.query(self.engine.SMSA_FILTER(), attributes)

        if verbose:
            self.display(entries, verbose)
        else:
            for entry in entries:
                sam = entry['sAMAccountName']
                for host in entry["msDS-HostServiceAccountBL"]:
                    print(f'{sam}:{host}')

    def list_shadow_principals(self, kwargs):
        """
        List the shadow principals and the groups associated with.

        Arguments:
            @verbose:bool
                Results will contain full information
        """
        try:
            verbose = kwargs.get("verbose", False)
            attributes = ALL if verbose else ["member", "msDS-ShadowPrincipalSid"]
            base = ','.join(["CN=Shadow Principal Configuration,CN=Services,CN=Configuration", self.engine.base_dn])
            entries = self.engine.query(self.engine.SHADOW_PRINCIPALS_FILTER(), attributes, base=base)

            if verbose:
                self.display(entries, verbose)
            else:
                for entry in entries:
                    print(f"User {entry['member'][0]} added to Group {format_sid(entry['msDS-ShadowPrincipalSid'])}")
        except:
            print("Can't retrieve shadow principals")

    def list_delegations(self, kwargs):
        """
        List accounts configured for any kind of delegation.

        Arguments:
            @verbose:bool
                Results will contain full information
            @filter:string = ["all", "unconstrained", "constrained", "rbcd"]
        """
        verbose = kwargs.get("verbose", False)
        filter_ = kwargs.get("filter", "all")

        attributes = ALL if verbose else ["sAMAccountName", "userAccountControl"]

        if filter_ == "all":
            if not verbose:
                attributes.extend([
                    "msDS-AllowedToDelegateTo",
                    "msDS-AllowedToActOnBehalfOfOtherIdentity"
                ])
            entries = self.engine.query(self.engine.ALL_DELEGATIONS_FILTER(), attributes)
        elif filter_ == "unconstrained":
            entries = self.engine.query(self.engine.UNCONSTRAINED_DELEGATION_FILTER(), attributes)
        elif filter_ == "constrained":
            if not verbose:
                attributes.append("msDS-AllowedToDelegateTo")
            entries = self.engine.query(self.engine.CONSTRAINED_DELEGATION_FILTER(), attributes)
        elif filter_ == "rbcd":
            if not verbose:
                attributes.append("msDS-AllowedToActOnBehalfOfOtherIdentity")
            entries = self.engine.query(self.engine.RESOURCE_BASED_CONSTRAINED_DELEGATION_FILTER(), attributes)
        else:
            return None

        if verbose:
            self.display(entries, verbose)
        else:
            for entry in entries:
                try:
                    uac = entry["userAccountControl"]
                    sam = entry["sAMAccountName"]
                    delegate = entry.get("msDS-AllowedToDelegateTo")
                    allowed_to_act = entry.get("msDS-AllowedToActOnBehalfOfOtherIdentity")
                    if (filter_ == "unconstrained" or filter_ == "all") and "TRUSTED_FOR_DELEGATION" in uac:
                        print(f"{sam}:unconstrained:")
                    if (filter_ == "constrained" or filter_ == "all") and delegate:
                        transition = "with" if "TRUSTED_TO_AUTH_FOR_DELEGATION" in uac else "without"
                        for a in delegate:
                            print(f"{sam}:constrained {transition} protocol transition:{a}")
                    if (filter_ == "rbcd" or filter_ == "all") and allowed_to_act:
                        sd = parse_ntSecurityDescriptor(allowed_to_act)
                        for ace in sd['DACL']['ACEs']:
                            try:
                                sid = ace.get('SID')
                                if not sid:
                                    continue
                                res = self.engine.resolve_sid(sid)
                                name = res[0]['sAMAccountName']
                                print(f"{name}:rbcd:{sam}")
                            except Exception:
                                print(f"{sid}:rbcd:{sam}")
                except Exception:
                    continue

    # GETTERS #

    def get_zone(self, kwargs):
        """
        Return the records of a DNS zone.

        Arguments:
            #dns_zone:string
                DNS zone to retrieve records
        """
        dns_zone = kwargs["dns_zone"]
        try:
            results = self.engine.query(
                self.engine.ZONE_FILTER(),
                base=','.join([f"DC={dns_zone}", "CN=MicrosoftDNS,DC=DomainDNSZones", self.engine.base_dn])
            )
        except LdapActiveDirectoryView.ActiveDirectoryLdapException as e:
            error(e)
        else:
            self.display(results)

    def get_membersof(self, kwargs):
        """
        List the members of `group`.

        Arguments:
            @verbose:bool
                Results will contain full information
            #group:string
                Group to list members
        """
        group = kwargs["group"]
        verbose = kwargs.get("verbose", False)

        results = list(self.engine.query(self.engine.GROUP_DN_FILTER(group), ["distinguishedName", "objectSid"]))
        if results:
            group_dn = results[0]["distinguishedName"]
        else:
            error("Group {group} does not exists".format(group=group))

        primary_group_id = results[0]["objectSid"].split('-')[-1]
        results = self.engine.query(self.engine.ACCOUNTS_IN_GROUP_FILTER(primary_group_id, group_dn))
        self.display(results, verbose)

    def get_memberships(self, kwargs):
        """
        List the group for which `account` belongs to.

        Arguments:
            #account:string
                User to list memberships
            @recursive:bool
                List recursively the groups
        """
        account = kwargs["account"]
        recursive = kwargs.get("recursive", False)

        already_printed = set()

        def lookup_groups(dn, leading_sp, already_treated):
            results = self.engine.query(self.engine.DISTINGUISHED_NAME(dn), ["memberOf", "primaryGroupID"])
            for result in results:
                if "memberOf" in result:
                    for group_dn in result["memberOf"]:
                        if group_dn not in already_treated:
                            print("{g:>{width}}".format(g=group_dn, width=leading_sp + len(group_dn)))
                            already_treated.add(group_dn)
                            lookup_groups(group_dn, leading_sp + 4, already_treated)

                if "primaryGroupID" in result and result["primaryGroupID"]:
                    pid = result["primaryGroupID"]
                    results = list(self.engine.query(self.engine.PRIMARY_GROUP_ID(pid)))
                    if results:
                        already_treated.add(results[0]["dn"])

            return already_treated

        results = self.engine.query(self.engine.ACCOUNT_IN_GROUPS_FILTER(account), ["memberOf", "primaryGroupID"])
        for result in results:
            if "memberOf" in result:
                for group_dn in result["memberOf"]:
                    print(group_dn)
                    if recursive:
                        already_printed.add(group_dn)
                        s = lookup_groups(group_dn, 4, already_printed)
                        already_printed.union(s)

            # for some reason, when we request an attribute which is not set on an object,
            # ldap3 returns an empty list as the value of this attribute
            if "primaryGroupID" in result and result["primaryGroupID"] != []:
                pid = result["primaryGroupID"]
                results = list(self.engine.query(self.engine.PRIMARY_GROUP_ID(pid)))
                if results:
                    print(results[0]["dn"])

    def get_from_sid(self, kwargs):
        """
        Return the object associated with the given `sid`.

        Arguments:
            @verbose:bool
                Results will contain full information
            #sid:string
                SID to search for
        """
        sid = kwargs["sid"]
        verbose = kwargs.get("verbose", False)

        try:
            result = self.engine.resolve_sid(sid)
            if isinstance(result, str):
                print(result)
            else:
                self.display(result, verbose)
        except ActiveDirectoryView.ActiveDirectoryInvalidSID:
            error("Invalid SID")

    def get_from_guid(self, kwargs):
        """
        Return the object associated with the given `guid`.

        Arguments:
            @verbose:bool
                Results will contain full information
            #guid:string
                GUID to search for
        """
        guid = kwargs["guid"]
        verbose = kwargs.get("verbose", False)

        try:
            self.display(self.engine.resolve_guid(guid), verbose)
        except ActiveDirectoryView.ActiveDirectoryLdapInvalidGUID:
            error("Invalid GUID")

    def get_laps(self, kwargs):
        """
        Return the LAPS passwords. If a target is specified, only retrieve the LAPS password for this one.

        Arguments:
            @verbose:bool
                Results will contain full information
            @computer:string
                Target computer where LAPS is set
        """
        computer = kwargs["computer"] if kwargs["computer"] else "*"
        verbose = kwargs.get("verbose", False)

        attributes = ALL if verbose else ["dNSHostName", "ms-Mcs-AdmPwd", "ms-Mcs-AdmPwdExpirationTime"]

        try:
            entries = self.engine.query(self.engine.LAPS_FILTER(computer), attributes)
            for entry in entries:
                if not verbose:
                    # TODO: deal with self.display for better code
                    cn = entry['dNSHostName']
                    password = entry['ms-Mcs-AdmPwd']
                    try:
                        epoch = (int(str(entry['ms-Mcs-AdmPwdExpirationTime'])) / 10000000) - 11644473600
                        expiration_date = datetime.fromtimestamp(epoch).strftime("%m-%d-%Y")
                    except Exception:
                        expiration_date = entry['ms-Mcs-AdmPwdExpirationTime']
                    print(f'{cn} {password} {expiration_date}')
                else:
                    self.display(entries, verbose)
        except Exception as e:
            error(e)

    def get_object(self, kwargs):
        """
        Return the records containing `object` in a CN.

        Arguments:
            @verbose:bool
                Results will contain full information
            #object:string
                Pattern to look for in CNs
        """
        anr = kwargs["object"]
        verbose = kwargs.get("verbose", False)

        if verbose:
            attributes = ALL
        else:
            attributes = ["sAMAccountName", "objectClass"]
        results = self.engine.query(self.engine.ANR(anr), attributes)
        self.display(results, verbose)

    def get_sddl(self, kwargs):
        """
        Returns the SDDL of an object given it's CN.

        Arguments:
            #object:string
                CN of object.
        """
        anr = kwargs["object"]

        results = self.engine.get_sddl(f"(anr={anr})")

        self.display(results, True, False)

    def get_silo(self, kwargs):
        """
        Get information about a specific `silo`.

        Arguments:
            #silo:string
                Silo to query
            @information:string = ["all", "members", "auth_policies"]
                Information to query
        """
        silo = kwargs["silo"]
        info = kwargs["information"]

        if info == "all":
            attributes = ALL
        elif info == "members":
            attributes = ["msDS-AuthNPolicySiloMembers"]
        elif info == "auth_policies":
            attributes = ["msDS-ComputerAuthNPolicy", "msDS-ServiceAuthNPolicy", "msDS-UserAuthNPolicy"]
        else:
            return None

        results = list(self.engine.query(
            self.engine.SILO_FILTER(silo),
            attributes,
            base=','.join(["CN=AuthN Policy Configuration,CN=Services,CN=Configuration", self.engine.base_dn]))
        )
        if not results:
            error(f"Silo {silo} does not exists")

        if info == "all":
            self.display(results, True)
        elif info == "members":
            members = results[0]["msDS-AuthNPolicySiloMembers"]
            print(*members, sep="\n")
        elif info == "auth_policies":
            print(f"msDS-ComputerAuthNPolicy: {results[0]['msDS-ComputerAuthNPolicy']}")
            print(f"msDS-ServiceAuthNPolicy: {results[0]['msDS-ServiceAuthNPolicy']}")
            print(f"msDS-UserAuthNPolicy: {results[0]['msDS-UserAuthNPolicy']}")

    # MISC #

    def misc_search(self, kwargs):
        """
        Query the LDAP with `filter` and retrieve ALL or `attributes` if specified.

        Arguments:
            #filter:string
                LDAP filter to search for
            #attributes:string = 'ALL'
                Comma separated list of attributes to display, ALL for every possible attribute
        """
        attr = kwargs["attributes"]
        filter_ = kwargs["filter"]

        try:
            if attr and attr != "ALL":
                results = self.engine.query(filter_, attr.split(","))
            else:
                results = self.engine.query(filter_)
            self.display(results, True)
        except PyAsn1UnicodeDecodeError as e:
            error(f"Decoding error with the filter: {e}")
        except Exception as e:
            if e.__str__() == "":
                error("An exception occurred with the provided filter")
            else:
                error(e)

    def misc_all(self, kwargs):
        """
        Collect and store computers, domain_policy, zones, gpo, groups, ou, users, trusts, pso information

        Arguments:
            #output:string
                File prefix for the files that will be created during the execution
        """
        output = kwargs["output"]
        kwargs["verbose"] = False

        for command, method in self.get_commands(prefix="list_"):
            info("Retrieving {command} output".format(command=command))
            if self.has_option(method, "filter"):
                filter_ = self.retrieve_default_val_for_arg(method, "filter")
                for f in filter_:
                    sys.stdout = Logger("{output}_{command}_{filter}.lst".format(output=output, command=command, filter=f), quiet=True)
                    kwargs["filter"] = f
                    getattr(self, method)(kwargs)

                    if self.has_option(method, "verbose"):
                        info("Retrieving {command} verbose output".format(command=command))
                        sys.stdout = Logger("{output}_{command}_{filter}.json".format(output=output, command=command, filter=f), quiet=True)
                        kwargs["verbose"] = True
                        getattr(self, method)(kwargs)
                        kwargs["verbose"] = False
                kwargs["filter"] = None
            else:
                sys.stdout = Logger("{output}_{command}.lst".format(output=output, command=command), quiet=True)
                getattr(self, method)(kwargs)

                if self.has_option(method, "verbose"):
                    info("Retrieving {command} verbose output".format(command=command))
                    sys.stdout = Logger("{output}_{command}.json".format(output=output, command=command), quiet=True)
                    kwargs["verbose"] = True
                    getattr(self, method)(kwargs)
                    kwargs["verbose"] = False

    def misc_enum_users(self, kwargs):
        """
        Anonymously enumerate users with LDAP pings.

        Arguments:
            #file:string
                File containing a list of usernames to try.
            @delay:int = 0
                Delay in milliseconds between each try.
        """

        # LDAP pings can only be used with an anonymous bind
        if self.engine.ldap.authentication != 'ANONYMOUS':
            error('The enum_users feature can only be used with an anonymous bind (-a option)')

        file = kwargs["file"]
        delay = kwargs["delay"]
        with open(file, 'r') as f:
            while True:
                line = f.readline()[:-1]
                if not line:
                    break
                if self.engine.user_exists(line):
                    print(line)
                sleep(delay / 1000)

    # ACTION #

    def action_unlock(self, kwargs):
        """
        Unlock `user`.

        Arguments:
            #user:string
                User to unlock
        """
        user = kwargs["user"]

        if self.engine.unlock(user):
            info("User {username} unlocked (or was already unlocked)".format(username=user))
        else:
            error("Unable to unlock {username}, check privileges".format(username=user))

    def action_modify_password(self, kwargs):
        """
        Change `user`'s password.

        Arguments:
            #user:string
                User to unlock
            #newpassword:string
                New password
            #currpassword:string = None
                Current password
        """
        user = kwargs["user"]
        new = kwargs["newpassword"]
        curr = kwargs.get("currpassword", None)
        if curr == "None":
            curr = None

        if self.engine.modify_password(user, curr, new):
            info("Password of {username} changed".format(username=user))
        else:
            error("Unable to change {username}'s password, check privileges or try with ldaps://".format(username=user))

    def action_add_to_group(self, kwargs):
        """
        Add `user` to `group`.

        Arguments:
            #user:string
                Target user (dn format). Ex: "CN=bob,CN=Users,DC=CORP,DC=LOCAL"
            #group:string
                Target group (dn format). Ex: "CN=Domain Admins,CN=Users,DC=CORP,DC=LOCAL"
        """
        user = kwargs["user"]
        group = kwargs["group"]

        if self.engine.add_user_to_group(user, group):
            info(f"User {user} sucessfully added to {group}")
        else:
            error(f"Unable to add {user} to {group}, check privileges or dn")

    def action_remove_from_group(self, kwargs):
        """
        Remove `user` from `group`.

        Arguments:
            #user:string
                Target user (dn format). Ex: "CN=bob,CN=Users,DC=CORP,DC=LOCAL"
            #group:string
                Target group (dn format). Ex: "CN=Domain Admins,CN=Users,DC=CORP,DC=LOCAL"
        """
        user = kwargs["user"]
        group = kwargs["group"]

        if self.engine.remove_user_from_group(user, group):
            info(f"User {user} sucessfully removed from {group}")
        else:
            error(f"Unable to remove {user} from {group}, check privileges or dn")

    def action_create_computer(self, kwargs):
        """
        Create a computer account

        Arguments:
            #computer_name:string
                Name of computer to add.
            #computer_pass:string
                Password set to computer account
        """
        computer = kwargs["computer_name"]
        password = kwargs["computer_pass"]

        try:
            self.engine.ldap.start_tls()
        except Exception as e:
            print(f"Can't create computer, TLS needed: {e}")
            return

        if self.engine.create_computer(computer, password):
            info(f"Computer {computer} sucessfully created wit password {password}")
        else:
            if self.engine.ldap.result['result'] == coreResults.RESULT_UNWILLING_TO_PERFORM:
                error_code = int(self.engine.ldap.result['message'].split(':')[0].strip(), 16)
                if error_code == 0x216D:
                    print(f"Machine quota exceeded with account {self.engine.username}")
                else:
                    print(str(self.engine.ldap.result))
            elif self.engine.ldap.result['result'] == coreResults.RESULT_INSUFFICIENT_ACCESS_RIGHTS:
                print(f"User {self.engine.username} doesn't have right to create a machine account!")
            elif self.engine.ldap.result['result'] == list(coreResults.RESULT_CODES.keys())[36]:
                print(f"Computer {computer} already exists")


def main():
    parser = ArgumentParser()
    parser.add_argument("-o", "--outfile", default="", help="Store the results in a file")
    parser.add_argument("--security_desc", action="store_true", help="Enable the retrieval of security descriptors in ldeep results")

    sub = parser.add_subparsers(title="Mode", dest="mode", description="Available modes", help="Backend engine to retrieve data")
    sub.required = True

    ldap = sub.add_parser("ldap", description="LDAP mode")
    cache = sub.add_parser("cache", description="Cache mode")

    ldap.add_argument("-d", "--domain", required=True, help="The domain as NetBIOS or FQDN")
    ldap.add_argument("-s", "--ldapserver", required=True, help="The LDAP path (ex : ldap://corp.contoso.com:389)")
    ldap.add_argument("-b", "--base", default="", help="LDAP base for query (by default, this value is pulled from remote Ldap)")
    ldap.add_argument("-t", "--type", default="ntlm", choices=['ntlm', 'simple'], help="Authentication type: ntlm (default) or simple")
    ldap.add_argument("--throttle", default=0, type=float, help="Add a throttle between queries to sneak under detection thresholds (in seconds between queries: argument to the sleep function)")
    ldap.add_argument("--page_size", default=1000, type=int, help="Configure the page size used by the engine to query the LDAP server (default: 1000)")

    cache.add_argument("-d", "--dir", default=".", type=str, help="Use saved JSON files in specified directory as cache")
    cache.add_argument("-p", "--prefix", required=True, type=str, help="Prefix of ldeep saved files")

    ntlm = ldap.add_argument_group("NTLM authentication")
    ntlm.add_argument("-u", "--username", help="The username")
    ntlm.add_argument("-p", "--password", help="The password used for the authentication")
    ntlm.add_argument("-H", "--ntlm", help="NTLM hashes, format is LMHASH:NTHASH")

    kerberos = ldap.add_argument_group("Kerberos authentication")
    kerberos.add_argument("-k", "--kerberos", action="store_true", help="For Kerberos authentication, ticket file should be pointed by $KRB5NAME env variable")

    certificate = ldap.add_argument_group("Certificate authentication")
    certificate.add_argument("--pfx-file", help="PFX file")
    certificate.add_argument("--cert-pem", help="User certificate")
    certificate.add_argument("--key-pem", help="User private key")

    anonymous = ldap.add_argument_group("Anonymous authentication")
    anonymous.add_argument("-a", "--anonymous", action="store_true", help="Perform anonymous binds")

    Ldeep.add_subparsers(ldap, "ldap", ["list_", "get_", "misc_", "action_"], title="commands", description="available commands")
    Ldeep.add_subparsers(cache, "cache", ["list_", "get_"], title="commands", description="available commands")

    args = parser.parse_args()

    # Output
    if args.outfile:
        sys.stdout = Logger(args.outfile, quiet=False)

    cache = "prefix" in args  # figuring out whether we use the cache or not

    # main
    if cache:
        try:
            query_engine = CacheActiveDirectoryView(args.dir, args.prefix)
        except CacheActiveDirectoryView.CacheActiveDirectoryDirNotFoundException as e:
            error(e)

    else:
        try:
            # Authentication
            method = "NTLM"
            if args.kerberos:
                method = "Kerberos"
            elif args.cert_pem or args.pfx_file:
                method = "Certificate"
            elif args.anonymous or args.command_ldap == "enum_users":
                method = "anonymous"
            elif args.type == "ntlm":
                method = "NTLM"
            elif args.type == "simple":
                method = "SIMPLE"
            else:
                error("Lack of authentication options: either Kerberos, Certificate, Username with Password (can be a NTLM hash) or Anonymous.")

            query_engine = LdapActiveDirectoryView(args.ldapserver, args.domain, args.base, args.username, args.password, args.ntlm, args.pfx_file, args.cert_pem, args.key_pem, method, args.throttle, args.page_size)

        except LdapActiveDirectoryView.ActiveDirectoryLdapException as e:
            error(e)

    # If `security_desc` are requested, enable LDAP Security Descriptor flags and modify the default attributes
    # In cache mode, the security_desc corresponding JSON field will be kept
    if args.security_desc:
        query_engine.set_controls(LDAP_SERVER_SD_FLAGS_OID_SEC_DESC)
        query_engine.set_all_attributes([ALL_ATTRIBUTES, ALL_OPERATIONAL_ATTRIBUTES, "ntSecurityDescriptor"])

    ldeep = Ldeep(query_engine)

    try:
        ldeep.dispatch_command(args)
    except CacheActiveDirectoryView.CacheActiveDirectoryException as e:
        error(e)
    except NotImplementedError:
        error("Feature not yet available")


if __name__ == "__main__":
    main()
