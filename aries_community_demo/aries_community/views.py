from django.http import HttpResponseBadRequest, HttpResponseRedirect, HttpResponse
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, get_user_model, login
from django.urls import reverse
from django.conf import settings
from django.utils.translation import ugettext_lazy as trans

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response

import pyqrcode
import uuid

from .forms import *
from .models import *
from .wallet_utils import *
from .registration_utils import *
from .agent_utils import *


USER_ROLE = getattr(settings, "DEFAULT_USER_ROLE", 'User')
ORG_ROLE = getattr(settings, "DEFAULT_ORG_ROLE", 'Admin')

###############################################################
# UI views to support user and organization registration
###############################################################

# Sign up as a site user, and create an agent
def user_signup_view(
    request,
    template=''
    ):
    """
    Create a user account with a managed agent.
    """

    if request.method == 'POST':
        form = UserSignUpForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get('email')
            raw_password = form.cleaned_data.get('password1')
            mobile_agent = form.cleaned_data.get('mobile_agent')

            user = authenticate(username=username, password=raw_password)

            if Group.objects.filter(name=USER_ROLE).exists():
                user.groups.add(Group.objects.get(name=USER_ROLE))
            user.save()

            # create an Indy agent - derive agent name from email, and re-use raw password
            user = user_provision(user, raw_password, mobile_agent=mobile_agent)

            # TODO need to auto-login with Atria custom user
            #login(request, user)

            return redirect('login')
    else:
        form = UserSignUpForm()
    return render(request, 'registration/signup.html', {'form': form})


# Sign up as an org user, and create a agent
def org_signup_view(
    request,
    template=''
    ):
    """
    Signup an Organization with a managed agent.
    Creates a user account and links to the Organization.
    """

    if request.method == 'POST':
        form = OrganizationSignUpForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get('email')
            raw_password = form.cleaned_data.get('password1')
            managed_agent = form.cleaned_data.get('managed_agent')
            admin_port = form.cleaned_data.get('admin_port')
            admin_endpoint = form.cleaned_data.get('admin_endpoint')
            http_port = form.cleaned_data.get('http_port')
            http_endpoint = form.cleaned_data.get('http_endpoint')
            api_key = form.cleaned_data.get('api_key')
            webhook_key = form.cleaned_data.get('webhook_key')

            user = authenticate(username=username, password=raw_password)
            user.managed_agent = False

            if Group.objects.filter(name='Admin').exists():
                user.groups.add(Group.objects.get(name='Admin'))
            user.save()

            # create and provision org, including org agent
            org_name = form.cleaned_data.get('org_name')
            org_role_name = form.cleaned_data.get('org_role_name')
            org_ico_url = form.cleaned_data.get('ico_url')
            org_role, created = AriesOrgRole.objects.get_or_create(name=org_role_name)
            org = org_signup(user, raw_password, org_name, org_role=org_role, org_ico_url=org_ico_url,
                managed_agent=managed_agent, admin_port=admin_port, admin_endpoint=admin_endpoint,
                http_port=http_port, http_endpoint=http_endpoint,
                api_key=api_key, webhook_key=webhook_key)

            # TODO need to auto-login with Atria custom user
            #login(request, user)

            return redirect('login')
    else:
        form = OrganizationSignUpForm()
    return render(request, 'registration/signup.html', {'form': form})


###############################################################
# Agent callback web service
###############################################################
TOPIC_CONNECTIONS = "connections"
TOPIC_CONNECTIONS_ACTIVITY = "connections_actvity"
TOPIC_CREDENTIALS = "issue_credential"
TOPIC_PRESENTATIONS = "present_proof"
TOPIC_GET_ACTIVE_MENU = "get-active-menu"
TOPIC_PERFORM_MENU_ACTION = "perform-menu-action"
TOPIC_PROBLEM_REPORT = "problem-report"

@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def agent_cb_view(
    request,
    cb_key,
    topic,
    format=None
    ):
    """
    Handle callbacks from the Aries agents.
    cb_key maps the callback to a specific agent.
    """
    payload = request.data
    agent = AriesAgent.objects.filter(callback_key=cb_key).get()

    if topic == TOPIC_CONNECTIONS:
        # handle connections callbacks
        return handle_agent_connections_callback(agent, topic, payload)

    elif topic == TOPIC_CONNECTIONS_ACTIVITY:
        # handle connections activity callbacks
        return handle_agent_connections_activity_callback(agent, topic, payload)

    elif topic == TOPIC_CREDENTIALS:
        # handle credentials callbacks
        return handle_agent_credentials_callback(agent, topic, payload)

    elif topic == TOPIC_PRESENTATIONS:
        # handle credentials callbacks
        return handle_agent_proof_callback(agent, topic, payload)

    # not yet handled message types
    print(">>> unhandled callback:", agent.agent_name, topic)
    return Response("{}")


###############################################################
# UI views to support Django wallet login/logoff
###############################################################
def agent_for_current_session(request):
    """
    Determine the current active agent
    """

    agent_name = request.session['agent_name']
    agent = AriesAgent.objects.filter(agent_name=agent_name).first()

    # validate it is the correct wallet
    agent_type = request.session['agent_type']
    agent_owner = request.session['agent_owner']
    if agent_type == 'user':
        # verify current user owns agent
        if agent_owner == request.user.email:
            return (agent, agent_type, agent_owner)
        raise Exception('Error agent/session config is not valid')
    elif agent_type == 'org':
        # verify current user has relationship to org that owns agent
        for org in request.user.ariesrelationship_set.all():
            if org.org.org_name == agent_owner:
                return (agent, agent_type, agent_owner)
        raise Exception('Error agent/session config is not valid')
    else:
        raise Exception('Error agent/session config is not valid')


###############################################################
# UI views to support wallet and agent UI functions
###############################################################
def profile_view(
    request,
    template='aries/profile.html'
    ):
    """
    List Connections for the current agent.
    """

# expects a agent to be opened in the current session
    (agent, agent_type, agent_owner) = agent_for_current_session(request)
    connections = AriesUser.objects.filter(email=agent_owner).all()

    return render(request, template,
              {'agent_name': agent.agent_name, 'connections': connections})


def data_view(
    request,
    template=''
    ):
    """
    Example of user-defined view for Data tab.
    """
    return render(request, 'aries/data.html')

def wallet_view(
   request
    ):
    """
    List info about wallet.
    """
    try:
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        wallets = get_wallet_dids(agent)
        print("Wallet->", wallets)
        return render(request, 'aries/wallet/list.html', {'agent_name': agent.agent_name, 'wallets': wallets})
    except:
        raise
    finally:
        pass

import importlib

def plugin_view(request, view_name):
    """
    Find and invoke user-defined view.
    These are configured in settings file.
    """

    view_function = getattr(settings, view_name)
    print(view_function)

    mod_name, func_name = view_function.rsplit('.',1)
    mod = importlib.import_module(mod_name)
    func = getattr(mod, func_name)

    return func(request)


######################################################################
# views to create and confirm agent-to-agent connections
######################################################################
def list_connections(
    request,
    template='aries/connection/list.html'
    ):
    """
    List Connections for the current agent.
    """

    # expects a agent to be opened in the current session
    (agent, agent_type, agent_owner) = agent_for_current_session(request)
    connections = AgentConnection.objects.filter(agent=agent).all()
    invitations = AgentInvitation.objects.filter(agent=agent, connecion_guid='').all()
    return render(request, template, {'agent_name': agent.agent_name, 'connections': connections, 'invitations': invitations})


def handle_connection_request_organization(
    request,
    form_template='aries/connection/request_org.html',
    response_template='aries/connection/form_connection_info_org.html'
    ):
    """
    Send a Connection request and approves automatic invitation from person to organization
    """

    if request.method=='POST':
        form = SendConnectionInvitationForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            id = cd.get('partner_name')
            partner_name_tmp = AriesOrganization.objects.filter(id=id).get()
            partner_name = partner_name_tmp

            # get user or org associated with this agent
            (agent, agent_type, agent_owner) = agent_for_current_session(request)

            if agent_type == 'user':
                org = AriesOrganization.objects.filter(org_name=partner_name).get()
                partner_name = agent_owner
            else:
                return render(request, response_template, {'msg': 'Invitations are available for org only', 'msg_txt': 'You are logged in as ' + agent_owner })

            # get user or org associated with target partner

            target_user = get_user_model().objects.filter(email=partner_name).all()
            target_org = AriesOrganization.objects.filter(org_name=partner_name).all()


            if 0 < len(target_user):
                their_agent = target_user[0].agent
            elif 0 < len(target_org):
                their_agent = target_org[0].agent
            else:
                their_agent = None

            # set agent password
            # TODO vcx_config['something'] = raw_password

            # build the connection and get the invitation data back
            try:
                my_connection = request_connection_invitation(org, partner_name)
                connecion_guid = my_connection.guid

                if their_agent is not None:

                    their_invitation = AgentInvitation(
                        agent = their_agent,
                        partner_name = partner_name_tmp,
                        invitation = my_connection.invitation,
                        invitation_url = my_connection.invitation_url,
                        )
                    their_invitation.save()

                invitations = AgentInvitation.objects.filter(id=their_invitation.id, agent=agent).all()
                agent_name = invitations[0].agent.agent_name

                # approves automatic invitation from person to organization
                partner_name = invitations[0].partner_name
                invitation_details = invitations[0].invitation
                (agent, agent_type, agent_owner) = agent_for_current_session(request)
                orgazinational_connection = receive_connection_invitation(agent, partner_name, invitation_details)
                connecion_guid = orgazinational_connection.guid
                invitation = AgentInvitation.objects.filter(id=their_invitation.id, agent=agent).get()

                invitation.connecion_guid = orgazinational_connection.guid
                invitation.save()

                if my_connection.agent.agent_org.get():
                    source_name = my_connection.agent.agent_org.get().org_name
                else:
                    source_name = my_connection.agent.agent_user.get().email
                target_name = my_connection.partner_name
                institution_logo_url = 'https://anon-solutions.ca/favicon.ico'
                return render(request, response_template, {
                    'msg':  trans('Created invitation for ') + target_name,
                    'msg_txt': my_connection.invitation,
                    'msg_txt2': their_invitation.id,
                    })

            except Exception as e:
                # ignore errors for now
                print(" >>> Failed to create request for", agent.agent_name)
                print(e)
                return render(request, 'aries/form_response.html', {'msg': trans('Failed to create invitation for')  + ' ' + agent.agent_name})

    else:
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        form = SendConnectionInvitationFormList(initial={'agent_name': agent.agent_name})
        return render(request, form_template, {'form': form})
    

def handle_connection_request(
    request,
    form_template='aries/connection/request.html',
    response_template='aries/connection/form_connection_info.html'
    ):
    """
    Send a Connection request (i.e. an Invitation).
    """

    if request.method=='POST':
        form = SendConnectionInvitationForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            partner_name = cd.get('partner_name')

            # get user or org associated with this agent
            (agent, agent_type, agent_owner) = agent_for_current_session(request)
            if agent_type == 'org':
                org = AriesOrganization.objects.filter(org_name=agent_owner).get()
            else:
                return render(request, response_template, {'msg': 'Invitations are available for org only', 'msg_txt': 'You are logged in as ' + agent_owner })

            # get user or org associated with target partner
            target_user = get_user_model().objects.filter(email=partner_name).all()
            target_org = AriesOrganization.objects.filter(org_name=partner_name).all()

            if 0 < len(target_user):
                their_agent = target_user[0].agent
            elif 0 < len(target_org):
                their_agent = target_org[0].agent
            else:
                their_agent = None

            # set agent password
            # TODO vcx_config['something'] = raw_password

            # build the connection and get the invitation data back
            try:
                my_connection = request_connection_invitation(org, partner_name)

                if their_agent is not None:
                    their_invitation = AgentInvitation(
                        agent = their_agent,
                        partner_name = agent_owner,
                        invitation = my_connection.invitation,
                        invitation_url = my_connection.invitation_url,
                        )
                    their_invitation.save()

                if my_connection.agent.agent_org.get():
                    source_name = my_connection.agent.agent_org.get().org_name
                else:
                    source_name = my_connection.agent.agent_user.get().email
                target_name = my_connection.partner_name
                institution_logo_url = 'https://anon-solutions.ca/favicon.ico'
                return render(request, response_template, {
                    'msg':  trans('Created invitation for ') + target_name, 
                    'msg_txt': my_connection.invitation,
                    'msg_txt2': their_invitation.id,
                    })
            except Exception as e:
                # ignore errors for now
                print(" >>> Failed to create request for", agent.agent_name)
                print(e)
                return render(request, 'aries/form_response.html', {'msg': trans('Failed to create invitation for')  + ' ' + agent.agent_name})

    else:
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        form = SendConnectionInvitationForm(initial={'agent_name': agent.agent_name})

        return render(request, form_template, {'form': form})
    

def handle_connection_response(
    request,
    form_template='aries/connection/response.html',
    response_template='aries/form_response.html'
    ):
    """
    Respond to (Accept) a Connection request.
    """

    if request.method=='POST':
        form = SendConnectionResponseForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': trans('Form error'), 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            invitation_id = cd.get('invitation_id')
            partner_name = cd.get('partner_name')
            invitation_details = cd.get('invitation_details')
            invitation_url = cd.get('invitation_url')

            # get user or org associated with this agent
            (agent, agent_type, agent_owner) = agent_for_current_session(request)

            # set agent password
            # TODO vcx_config['something'] = raw_password

            # build the connection and get the invitation data back
            try:
                my_connection = receive_connection_invitation(agent, partner_name, invitation_details)

                invitation = AgentInvitation.objects.filter(id=invitation_id, agent=agent).get()
                invitation.connecion_guid = my_connection.guid
                invitation.save()

                return render(request, response_template, {'msg': 'Updated connection for ' + agent.agent_name})
            except IndyError:
                # ignore errors for now
                print(" >>> Failed to update request for", agent.agent_name)
                return render(request, 'aries/form_response.html', {'msg': 'Failed to update request for ' + agent.agent_name})

    else:
        # find connection request
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        invitation_id = request.GET.get('id', None)
        invitations = []
        if invitation_id:
            invitations = AgentInvitation.objects.filter(id=invitation_id, agent=agent).all()
        if len(invitations) > 0:
            form = SendConnectionResponseForm(initial={ 'invitation_id': invitation_id,
                                                        'agent_name': invitations[0].agent.agent_name, 
                                                        'partner_name': invitations[0].partner_name, 
                                                        'invitation_details': invitations[0].invitation,
                                                        'invitation_url': invitations[0].invitation_url,
                                                         })
        else:
            (agent, agent_type, agent_owner) = agent_for_current_session(request)
            form = SendConnectionResponseForm(initial={'invitation_id': 0, 'agent_name': agent.agent_name})

        return render(request, form_template, {'form': form, 'invitation_id': invitation_id})
    

def poll_connection_status(
    request,
    form_template='aries/connection/status.html',
    response_template='aries/form_response.html'
    ):
    """
    Poll Connection status (normally a background task).
    """

    if request.method=='POST':
        form = PollConnectionStatusForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            connection_id = cd.get('connection_id')

            # log out of current agent, if any
            (agent, agent_type, agent_owner) = agent_for_current_session(request)

            # set agent password
            # TODO vcx_config['something'] = raw_password

            connections = AgentConnection.objects.filter(guid=connection_id, agent=agent).all()
            # TODO validate connection id
            my_connection = connections[0]

            # validate connection and get the updated status
            try:
                my_state = check_connection_status(agent, my_connection.guid)

                return render(request, response_template, {'msg': 'Updated connection for ' + agent.agent_name + ', ' + my_connection.partner_name})
            except Exception as e:
                # ignore errors for now
                print(" >>> Failed to update request for", agent.agent_name, e)
                return render(request, 'aries/form_response.html', {'msg': 'Failed to update request for ' + agent.agent_name})

    else:
        # find connection request
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        connection_id = request.GET.get('id', None)
        connections = AgentConnection.objects.filter(guid=connection_id, agent=agent).all()

        form = PollConnectionStatusForm(initial={ 'connection_id': connection_id,
                                                  'agent_name': connections[0].agent.agent_name })

        return render(request, form_template, {'form': form})


def connection_qr_code(
    request, 
    token
    ):
    """
    Display a QR code for the given invitation.
    """

    # find connection for requested token
    connections = AgentInvitation.objects.filter(id=token).all()
    if 0 == len(connections):
        return render(request, 'aries/form_response.html', {'msg': 'No connection found'})

    connection = connections[0]
    qr = pyqrcode.create(connection.invitation_url)
    path_to_image = '/tmp/'+token+'-qr-offer.png'
    qr.png(path_to_image, scale=2, module_color=[0, 0, 0, 128], background=[0xff, 0xff, 0xff])
    image_data = open(path_to_image, "rb").read()

    # serialize to HTTP response
    response = HttpResponse(image_data, content_type="image/png")
    #image.save(response, "PNG")
    return response


######################################################################
# views to offer, request, send and receive credentials
######################################################################
def check_connection_messages(
    request,
    form_template='aries/connection/check_messages.html',
    response_template='aries/form_response.html'
    ):
    """
    Poll Connections for outstanding messages (normally a background task).
    """

    if request.method=='POST':
        form = PollConnectionStatusForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            connection_id = cd.get('connection_id')

            # log out of current wallet, if any
            (agent, agent_type, agent_owner) = agent_for_current_session(request)
    
            if connection_id > 0:
                connections = AgentConnection.objects.filter(wallet=wallet, id=connection_id).all()
            else:
                connections = AgentConnection.objects.filter(wallet=wallet).all()

            total_count = 0
            for connection in connections:
                # check for outstanding, un-received messages - add to outstanding conversations
                if connection.connection_type == 'Inbound':
                    msg_count = handle_inbound_messages(wallet, connection)
                    total_count = total_count + msg_count

            return render(request, response_template, {'msg': 'Received message count = ' + str(total_count)})

    else:
        # find connection request
        connection_id = request.GET.get('connection_id', None)
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        if connection_id:
            connections = AgentConnection.objects.filter(wallet=wallet, id=connection_id).all()
        else:
            connection_id = 0
            connections = AgentConnection.objects.filter(wallet=wallet).all()
        # TODO validate connection id
        form = PollConnectionStatusForm(initial={ 'connection_id': connection_id,
                                                  'wallet_name': connections[0].wallet.wallet_name })

        return render(request, form_template, {'form': form})


def list_conversations(
    request,
    template='aries/conversation/list.html'
    ):
    """
    List Conversations for the current wallet.
    """

    # expects a wallet to be opened in the current session
    (agent, agent_type, agent_owner) = agent_for_current_session(request)
    conversations = AgentConversation.objects.filter(connection__agent=agent).all()
    return render(request, template, {'agent_name': agent.agent_name, 'conversations': conversations})


def handle_select_credential_offer(
    request,
    form_template='aries/credential/select_offer.html',
    response_template='aries/credential/offer.html'
    ):
    """
    Select a Credential Definition and display a form to enter Credential Offer information.
    """

    if request.method=='POST':
        form = SelectCredentialOfferForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            connection_id = cd.get('connection_id')
            cred_def = cd.get('cred_def')
            partner_name = cd.get('partner_name')

            credential_name = cred_def.creddef_name
            cred_def_id = cred_def.ledger_creddef_id

            # log out of current wallet, if any
            (agent, agent_type, agent_owner) = agent_for_current_session(request)

            connections = AgentConnection.objects.filter(guid=connection_id, agent=agent).all()
            # TODO validate connection id
            schema_attrs = cred_def.creddef_template
            form = SendCredentialOfferForm(initial={ 'connection_id': connection_id,
                                                     'agent_name': connections[0].agent.agent_name,
                                                     'partner_name': partner_name,
                                                     'cred_def': cred_def_id,
                                                     'schema_attrs': schema_attrs,
                                                     'credential_name': credential_name })

            return render(request, response_template, {'form': form})

    else:
        # find conversation request
        connection_id = request.GET.get('connection_id', None)
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        connections = AgentConnection.objects.filter(guid=connection_id, agent=agent).all()
        # TODO validate connection id
        form = SelectCredentialOfferForm(initial={ 'connection_id': connection_id,
                                                   'partner_name': connections[0].partner_name,
                                                   'agent_name': connections[0].agent.agent_name})

        return render(request, form_template, {'form': form})


def handle_credential_offer(
    request,
    template='aries/form_response.html'
    ):
    """
    Send a Credential Offer.
    """

    if request.method=='POST':
        form = SendCredentialOfferForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            connection_id = cd.get('connection_id')
            cred_def_id = cd.get('cred_def')
            credential_name = cd.get('credential_name')
            schema_attrs = cd.get('schema_attrs')
            schema_attrs = json.loads(schema_attrs)
            cred_attrs = []
            for attr in schema_attrs:
                field_name = 'schema_attr_' + attr
                field_value = request.POST.get(field_name)
                cred_attrs.append({"name": attr, "value": request.POST.get(field_name)})

            (agent, agent_type, agent_owner) = agent_for_current_session(request)
    
            connections = AgentConnection.objects.filter(guid=connection_id, agent=agent).all()
            # TODO validate connection id
            my_connection = connections[0]

            cred_defs = IndyCredentialDefinition.objects.filter(ledger_creddef_id=cred_def_id, agent=agent).all()
            cred_def = cred_defs[0]

            # set wallet password
            # TODO vcx_config['something'] = raw_password

            # build the credential offer and send
            try:
                my_conversation = send_credential_offer(agent, my_connection, cred_attrs, cred_def_id)

                return render(request, template, {'msg': 'Updated conversation for ' + agent.agent_name})
            except:
                # ignore errors for now
                print(" >>> Failed to update conversation for", agent.agent_name)
                return render(request, 'aries/form_response.html', {'msg': 'Failed to update conversation for ' + agent.agent_name})

    else:
        return render(request, 'aries/form_response.html', {'msg': 'Method not allowed'})


def handle_cred_offer_response(
    request,
    form_template='aries/credential/offer_response.html',
    response_template='aries/form_response.html'
    ):
    """
    Respond to a Credential Offer by sending a Credential Request.
    """

    if request.method=='POST':
        form = SendCredentialResponseForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            conversation_id = cd.get('conversation_id')

            (agent, agent_type, agent_owner) = agent_for_current_session(request)
    
            # find conversation request
            conversations = AgentConversation.objects.filter(guid=conversation_id, connection__agent=agent).all()
            my_conversation = conversations[0]
            # TODO validate conversation id
            my_connection = my_conversation.connection

            # build the credential request and send
            try:
                my_conversation = send_credential_request(agent, my_conversation)

                return render(request, response_template, {'msg': 'Updated conversation for ' + agent.agent_name})
            except:
                # ignore errors for now
                print(" >>> Failed to update conversation for", agent.agent_name)
                return render(request, 'aries/form_response.html', {'msg': 'Failed to update conversation for ' + agent.agent_name})

    else:
        # find conversation request, fill in form details
        conversation_id = request.GET.get('conversation_id', None)
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        conversations = AgentConversation.objects.filter(guid=conversation_id, connection__agent=agent).all()
        # TODO validate conversation id
        conversation = conversations[0]
        agent_conversation = get_agent_conversation(agent, conversation_id, CRED_EXCH_CONVERSATION)
        print("agent_conversation:", agent_conversation)
        proposed_attrs = agent_conversation["credential_proposal_dict"]["credential_proposal"]["attributes"]
        cred_attrs = {}
        for i in range(len(proposed_attrs)):
            cred_attrs[proposed_attrs[i]["name"]] = proposed_attrs[i]["value"]
        # TODO validate connection id
        connection = conversation.connection
        form = SendCredentialResponseForm(initial={ 
                                                 'conversation_id': conversation_id,
                                                 'agent_name': connection.agent.agent_name,
                                                 'from_partner_name': connection.partner_name,
                                                 'claim_name': agent_conversation['credential_definition_id'],
                                                 'credential_attrs': cred_attrs,
                                                 'libindy_offer_schema_id': agent_conversation['schema_id']
                                                })

        return render(request, form_template, {'form': form})


######################################################################
# views to request, send and receive proofs
######################################################################
def handle_select_proof_request(
    request,
    form_template='aries/proof/select_request.html',
    response_template='aries/proof/send_request.html'
    ):
    """
    Select a Proof Request to send, based on the templates available in the database.
    """

    if request.method=='POST':
        form = SelectProofRequestForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            proof_request = cd.get('proof_request')
            connection_id = cd.get('connection_id')
            partner_name = cd.get('partner_name')

            (agent, agent_type, agent_owner) = agent_for_current_session(request)

            connections = AgentConnection.objects.filter(guid=connection_id, agent=agent).all()
            # TODO validate connection id
            connection = connections[0]

            proof_req_attrs = proof_request.proof_req_attrs
            proof_req_predicates = proof_request.proof_req_predicates

            # selective attribute substitutions
            institution_did = get_public_did(agent)
            proof_req_attrs = proof_req_attrs.replace('$ISSUER_DID', institution_did)
            proof_req_predicates = proof_req_predicates.replace('$ISSUER_DID', institution_did)

            proof_form = SendProofRequestForm(initial={
                    'agent_name': connection.agent.agent_name,
                    'connection_id': connection_id,
                    'partner_name': partner_name,
                    'proof_name': proof_request.proof_req_name,
                    'proof_attrs': proof_req_attrs,
                    'proof_predicates': proof_req_predicates})

            return render(request, response_template, {'form': proof_form})

    else:
        # find conversation request
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        connection_id = request.GET.get('connection_id', None)
        connection = AgentConnection.objects.filter(guid=connection_id, agent=agent).get()

        form = SelectProofRequestForm(initial={ 'connection_id': connection_id,
                                                'partner_name': connection.partner_name,
                                                'agent_name': connection.agent.agent_name })

        return render(request, form_template, {'form': form})


def handle_send_proof_request(
    request,
    template='aries/form_response.html'
    ):
    """
    Send a Proof Request for the selected Proof Request.
    User can edit the requested attributes and predicates.
    """

    if request.method=='POST':
        form = SendProofRequestForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            connection_id = cd.get('connection_id')
            proof_name = cd.get('proof_name')
            proof_attrs = cd.get('proof_attrs')
            proof_predicates = cd.get('proof_predicates')

            (agent, agent_type, agent_owner) = agent_for_current_session(request)
    
            connections = AgentConnection.objects.filter(guid=connection_id, agent=agent).all()
            # TODO validate connection id
            my_connection = connections[0]

            proof_req_attrs = json.loads(proof_attrs)
            proof_req_predicates = json.loads(proof_predicates)

            requested_attrs = {}
            for requested_attr in proof_req_attrs:
                referent = requested_attr["name"] + "_referent"
                requested_attrs[referent] = requested_attr
            requested_predicates = {}
            for requested_predicate in proof_req_predicates:
                referent = requested_predicate["name"] + "_referent"
                requested_predicates[referent] = requested_predicate

            # build the proof request and send
            try:
                conversation = send_proof_request(agent, my_connection, proof_name, requested_attrs, requested_predicates)

                return render(request, template, {'msg': 'Updated conversation for ' + agent.agent_name})
            except:
                # ignore errors for now
                print(" >>> Failed to update conversation for", agent.agent_name)
                return render(request, 'aries/form_response.html', {'msg': 'Failed to update conversation for ' + agent.agent_name})

    else:
        return render(request, 'aries/form_response.html', {'msg': 'Method not allowed'})


def handle_proof_req_response(
    request,
    form_template='aries/proof/send_response.html',
    response_template='aries/proof/select_claims.html'
    ):
    """
    First stage in responding to a Proof Request - confirm to search for claims.
    """

    if request.method=='POST':
        form = SendProofReqResponseForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            conversation_id = cd.get('conversation_id')
            proof_req_name = cd.get('proof_req_name')

            (agent, agent_type, agent_owner) = agent_for_current_session(request)
   
            # find conversation request
            conversations = AgentConversation.objects.filter(guid=conversation_id, connection__agent=agent).all()
            my_conversation = conversations[0]
            # TODO validate conversation id
            # TODO validate connection id
            my_connection = my_conversation.connection

            # find claims for this proof request and display for the user
            try:
                proof_request = get_agent_conversation(agent, conversation_id, PROOF_REQ_CONVERSATION)
                claim_data = get_claims_for_proof_request(agent, my_conversation)

                form = SelectProofReqClaimsForm(initial={
                         'conversation_id': conversation_id,
                         'agent_name': my_connection.agent.agent_name,
                         'from_partner_name': my_connection.partner_name,
                         'proof_req_name': proof_req_name,
                         'selected_claims': claim_data,
                         'proof_request': proof_request,
                    })

                return render(request, response_template, {'form': form})
            except Exception as e:
                # ignore errors for now
                print(" >>> Failed to find claims for", agent.agent_name, e)
                return render(request, 'aries/form_response.html', {'msg': 'Failed to find claims for ' + agent.agent_name})

    else:
        # find conversation request, fill in form details
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        conversation_id = request.GET.get('conversation_id', None)
        conversations = AgentConversation.objects.filter(guid=conversation_id, connection__agent=agent).all()
        # TODO validate conversation id
        conversation = conversations[0]
        # TODO validate connection id
        connection = conversation.connection
        proof_request = get_agent_conversation(agent, conversation_id, PROOF_REQ_CONVERSATION)
        print("proof_request:", proof_request)
        form = SendProofReqResponseForm(initial={ 
                                                 'conversation_id': conversation_id,
                                                 'agent_name': agent.agent_name,
                                                 'from_partner_name': connection.partner_name,
                                                 'proof_req_name': proof_request['presentation_request']['name'],
                                                })

    return render(request, form_template, {'form': form})


def handle_proof_select_claims(
    request,
    template='aries/form_response.html'
    ):
    """
    Select claims to construct Proof for Proof Request.
    """

    if request.method=='POST':
        form = SelectProofReqClaimsForm(request.POST)
        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            cd = form.cleaned_data
            conversation_id = cd.get('conversation_id')
            proof_req_name = cd.get('proof_req_name')

            (agent, agent_type, agent_owner) = agent_for_current_session(request)

            # find conversation request
            conversations = AgentConversation.objects.filter(guid=conversation_id, connection__agent=agent).all()
            # TODO validate conversation id
            my_conversation = conversations[0]
            requested_proof = get_agent_conversation(agent, conversation_id, PROOF_REQ_CONVERSATION)
            # TODO validate connection id
            my_connection = my_conversation.connection

            # get selected attributes for proof request
            supplied_attrs = {}
            supplied_predicates = {}
            supplied_self_attested_attrs = {}

            # build array of credential id's (from wallet)
            for referent in requested_proof["presentation_request"]["requested_attributes"]:
                field_name = 'proof_req_attr_' + referent
                value = request.POST.get(field_name)
                if value.startswith('ref::'):
                    supplied_attrs[referent] = { "cred_id": value[5:], "revealed": True }
                else:
                    supplied_self_attested_attrs[referent] = value
            for referent in requested_proof["presentation_request"]["requested_predicates"]:
                field_name = 'proof_req_attr_' + referent
                value = request.POST.get(field_name)
                if value.startswith('ref::'):
                    supplied_predicates[referent] = { "cred_id": value[5:] }
                else:
                    # shouldn't happen ...
                    supplied_predicates[referent] = { "cred_id": value }

            # send claims for this proof request to requestor
            try:
                proof_data = send_claims_for_proof_request(agent, my_conversation, supplied_attrs, supplied_predicates, supplied_self_attested_attrs)

                return render(request, template, {'msg': 'Sent proof request for ' + agent.agent_name})
            except Exception as e:
                # ignore errors for now
                print(" >>> Failed to find claims for", agent.agent_name, e)
                return render(request, 'aries/form_response.html', {'msg': 'Failed to find claims for ' + agent.agent_name})

    else:
        return render(request, 'aries/form_response.html', {'msg': 'Method not allowed'})


def handle_view_proof(
    request,
    template='aries/proof/view_proof.html'
    ):
    """
    View the Proof sent by the Prover.
    """

    (agent, agent_type, agent_owner) = agent_for_current_session(request)
    conversation_id = request.GET.get('conversation_id', None)
    conversations = AgentConversation.objects.filter(guid=conversation_id, connection__agent=agent).all()
    # TODO validate conversation id
    conversation = conversations[0]

    requested_proof = get_agent_conversation(agent, conversation_id, PROOF_REQ_CONVERSATION)

    for attr, value in requested_proof["presentation"]["requested_proof"]["revealed_attrs"].items():
        value["identifier"] = requested_proof["presentation"]["identifiers"][value["sub_proof_index"]]
    for attr, value in requested_proof["presentation"]["requested_proof"]["predicates"].items():
        value["identifier"] = requested_proof["presentation"]["identifiers"][value["sub_proof_index"]]

    return render(request, template, {'conversation': conversation, 'proof': requested_proof})


######################################################################
# views to list wallet credentials
######################################################################
def form_response(request):
    """
    Generic response page.
    """

    msg = request.GET.get('msg', None)
    msg_txt = request.GET.get('msg_txt', None)
    return render(request, 'aries/form_response.html', {'msg': msg, 'msg_txt': msg_txt})


def list_wallet_credentials(
    request
    ):
    """
    List all credentials in the current wallet.
    """

    try:
        (agent, agent_type, agent_owner) = agent_for_current_session(request)

        credentials = fetch_credentials(agent)

        return render(request, 'aries/credential/list.html', {'agent_name': agent.agent_name, 'credentials': credentials})
    except:
        raise
    finally:
        pass

#Remove connection in database
def handle_remove_connection(
    request,
#    form_template='aries/connection/select_request.html',
    form_template='aries/connection/form_remove_connection.html',
    response_template='aries/connection/list.html'
    ):
    """
    Select a Proof Request to send, based on the templates available in the database.
    """

    if request.method=='POST':
        form = RemoveConnectionForm(request.POST)

        if not form.is_valid():
            return render(request, 'aries/form_response.html', {'msg': 'Form error', 'msg_txt': str(form.errors)})
        else:
            (agent, agent_type, agent_owner) = agent_for_current_session(request)

            cd = form.cleaned_data
            connection_id = cd.get('connection_id')
            partner_name = cd.get('partner_name')
            agent_name = cd.get('agent_name')

            print('agent->', agent_owner)
            print('agent_name->', agent_name)

            guid_partner_name = AgentConnection.objects.filter(partner_name=partner_name, agent=agent_name).get()
            guid_partner_name.delete()
            agent_org = AriesOrganization.objects.filter(org_name=partner_name).get()
            guid_partner_agent_owner = AgentConnection.objects.filter(partner_name=agent_owner, agent=agent_org.agent).get()
            guid_partner_agent_owner.delete()

            return list_connections(
                request,
                template='aries/connection/list.html'
            )

    else:
        # find conversation request
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        connection_id = request.GET.get('connection_id', None)
        connection = AgentConnection.objects.filter(guid=connection_id, agent=agent).get()

        form = RemoveConnectionForm(initial={'connection_id': connection_id,
                                               'partner_name': connection.partner_name,
                                               'agent_name': connection.agent.agent_name})

        return render(request, form_template, {'form': form})

#Function created to allow updating of information in the user's profile
def handle_update_user(
    request,
    form_template='aries/request_update.html',
    response_template='aries/profile.html'
    ):
    (agent, agent_type, agent_owner) = agent_for_current_session(request)
    connections = AriesUser.objects.get(email=agent_owner)

    if request.method == 'POST':
        form = UserUpdateForm(request.POST, request.FILES)

        if form.is_valid():
            cd = form.cleaned_data
            connections.first_name = cd.get('first_name')
            connections.last_name = cd.get('last_name')
            connections.date_birth = cd.get('date_birth')
            ori_photo = cd.get('ori_photo')
            new_photo = cd.get('new_photo')
            password1 = cd.get('password1')

            if new_photo is None:
                connections.photo = cd.get('ori_photo')
            else:
                connections.photo = cd.get('new_photo')

            if password1 != '':
                connections.set_password(password1)

        connections.save()

        connections = AriesUser.objects.filter(email=agent_owner).all()
        return render(request, response_template,
                      {'agent_name': agent.agent_name, 'connections': connections})

    else:
        (agent, agent_type, agent_owner) = agent_for_current_session(request)
        form = UserUpdateForm(initial={'agent_name': agent})
        return render(request, form_template, {'form': form})
