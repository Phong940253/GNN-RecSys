import datetime
from datetime import timedelta
import logging
import math
import time

import click
import numpy as np
from skopt import gp_minimize
from skopt.space import Real, Integer, Categorical
from skopt.utils import use_named_args
from skopt.callbacks import CheckpointSaver
from skopt import load
import torch

from src.builder import create_graph, import_features
from src.model import ConvModel, max_margin_loss
from src.sampling import train_valid_split, generate_dataloaders
from src.metrics import (create_already_bought, create_ground_truth,
                         get_metrics_at_k, get_recs)
from src.train.run import train_model, get_embeddings
from src.evaluation import explore_recs, explore_sports, check_coverage
from src.utils import save_txt, save_outputs, get_last_checkpoint
from src.utils_data import DataLoader, FixedParameters, DataPaths, assign_graph_features
from src.utils_vizualization import plot_train_loss
import inference_hp

from logging_config import get_logger

log = get_logger(__name__)

global cuda

cuda = torch.cuda.is_available()
device = torch.device('cuda')
if not cuda:
    num_workers = 0
else:
    num_workers = 4


def train(data, fixed_params, data_paths,
          visualization, check_embedding, **params):
    """
    Function to find the best hyperparameter combination.

    Files needed to run
    -------------------
    All the files in the src.utils_data.DataPaths:
        It includes all the interactions between user, sport and items, as well as features for user, sport and items.
    If starting hyperparametrization from a checkpoint:
        The checkpoint file, generated by skopt during a previous hyperparametrization. The most recent file of
        the root folder will be fetched.

    Parameters
    ----------
    data :
        Object of class DataLoader, containing multiple arguments such as user_item_train dataframe, graph schema, etc.
    fixed_params :
        All parameters that are fixed, i.e. not part of the hyperparametrization.
    data_paths :
        All data paths (mainly csv).  # Note: currently, only paths.result_filepath is used here.
    visualization :
        Visualize results or not.  # Note: currently not used, visualization is always on or controlled by fixed_params.
    check_embedding :
        Visualize recommendations or not.  # Note: currently not used, controlled by fixed_params.
    **params :
        Mainly params that come from the hyperparametrization loop, controlled by skopt.

    Returns
    -------
    recall :
        Recall on the test set for the current combination of hyperparameters.

    Saves to files
    --------------
    logging of all experiments:
        All training logs are saved to result_filepath, including losses, metrics and examples of recommendations
        Plots of the evolution of losses and metrics are saved to the folder 'plots'
    best models:
        All models, fixed_params and params that yielded recall higher than 8% on specific item identifier or 20% on
        generic item identifier are saved to the folder 'models'
    """
    # Establish hyperparameters
    # Dimensions
    out_dim = {'Very Small': 32, 'Small': 96, 'Medium': 128, 'Large': 192, 'Very Large': 256}
    hidden_dim = {'Very Small': 64, 'Small': 192, 'Medium': 256, 'Large': 384, 'Very Large': 512}
    params['out_dim'] = out_dim[params['embed_dim']]
    params['hidden_dim'] = hidden_dim[params['embed_dim']]

    # Popularity
    use_popularity = {'No': False, 'Small': True, 'Medium': True, 'Large': True}
    weight_popularity = {'No': 0, 'Small': .01, 'Medium': .05, 'Large': .1}
    days_popularity = {'No': 0, 'Small': 7, 'Medium': 7, 'Large': 7}
    params['use_popularity'] = use_popularity[params['popularity_importance']]
    params['weight_popularity'] = weight_popularity[params['popularity_importance']]
    params['days_popularity'] = days_popularity[params['popularity_importance']]

    if fixed_params.duplicates == 'count_occurrence':
        params['aggregator_type'] += '_edge'

    # Make sure graph data is consistent with message passing parameters
    if fixed_params.duplicates == 'count_occurrence':
        assert params['aggregator_type'].endswith('edge')
    else:
        assert not params['aggregator_type'].endswith('edge')

    valid_graph = create_graph(
        data.graph_schema,
    )
    valid_graph = assign_graph_features(valid_graph,
                                        fixed_params,
                                        data,
                                        **params,
                                        )

    dim_dict = {'user': valid_graph.nodes['user'].data['features'].shape[1],
                'item': valid_graph.nodes['item'].data['features'].shape[1],
                'out': params['out_dim'],
                'hidden': params['hidden_dim']}

    all_sids = None
    # if 'sport' in valid_graph.ntypes:
    #     dim_dict['sport'] = valid_graph.nodes['sport'].data['features'].shape[1]
    #     all_sids = np.arange(valid_graph.num_nodes('sport'))

    # get training and test ids
    (
        train_graph,
        train_eids_dict,
        valid_eids_dict,
        subtrain_uids,
        valid_uids,
        test_uids,
        all_iids,
        ground_truth_subtrain,
        ground_truth_valid,
        all_eids_dict
    ) = train_valid_split(
        valid_graph,
        data.ground_truth_test,
        fixed_params.etype,
        fixed_params.subtrain_size,
        fixed_params.valid_size,
        fixed_params.reverse_etype,
        fixed_params.train_on_clicks,
        fixed_params.remove_train_eids,
        params['clicks_sample'],
        params['purchases_sample'],
    )

    (
        edgeloader_train,
        edgeloader_valid,
        nodeloader_subtrain,
        nodeloader_valid,
        nodeloader_test
    ) = generate_dataloaders(valid_graph,
                             train_graph,
                             train_eids_dict,
                             valid_eids_dict,
                             subtrain_uids,
                             valid_uids,
                             test_uids,
                             all_iids,
                             fixed_params,
                             num_workers,
                             all_sids,
                             embedding_layer=params['embedding_layer'],
                             n_layers=params['n_layers'],
                             neg_sample_size=params['neg_sample_size'],
                             )

    train_eids_len = 0
    valid_eids_len = 0
    for etype in train_eids_dict.keys():
        train_eids_len += len(train_eids_dict[etype])
        valid_eids_len += len(valid_eids_dict[etype])
    num_batches_train = math.ceil(train_eids_len / fixed_params.edge_batch_size)
    num_batches_subtrain = math.ceil(
        (len(subtrain_uids) + len(all_iids)) / fixed_params.node_batch_size
    )
    num_batches_val_loss = math.ceil(valid_eids_len / fixed_params.edge_batch_size)
    num_batches_val_metrics = math.ceil(
        (len(valid_uids) + len(all_iids)) / fixed_params.node_batch_size
    )
    num_batches_test = math.ceil(
        (len(test_uids) + len(all_iids)) / fixed_params.node_batch_size
    )

    if fixed_params.neighbor_sampler == 'partial':
        params['n_layers'] = 3

    model = ConvModel(valid_graph,
                      params['n_layers'],
                      dim_dict,
                      params['norm'],
                      params['dropout'],
                      params['aggregator_type'],
                      fixed_params.pred,
                      params['aggregator_hetero'],
                      params['embedding_layer'],
                      )
    if cuda:
        model = model.to(device)

    hp_sentence = params
    hp_sentence.update(vars(fixed_params))
    hp_sentence.update(
        {
            'cuda': cuda,
        }
    )
    hp_sentence = f'{str(hp_sentence)[1: -1]} \n'

    save_txt(f'\n \n START - Hyperparameters \n{hp_sentence}', data_paths.result_filepath, "a")

    start_time = time.time()

    # Train model
    trained_model, viz, best_metrics = train_model(
        model,
        fixed_params.num_epochs,
        num_batches_train,
        num_batches_val_loss,
        edgeloader_train,
        edgeloader_valid,
        max_margin_loss,
        params['delta'],
        params['neg_sample_size'],
        params['use_recency'],
        cuda,
        device,
        fixed_params.optimizer,
        params['lr'],
        get_metrics=True,
        train_graph=train_graph,
        valid_graph=valid_graph,
        nodeloader_valid=nodeloader_valid,
        nodeloader_subtrain=nodeloader_subtrain,
        k=fixed_params.k,
        out_dim=params['out_dim'],
        num_batches_val_metrics=num_batches_val_metrics,
        num_batches_subtrain=num_batches_subtrain,
        bought_eids=train_eids_dict[('user', 'buys', 'item')],
        ground_truth_subtrain=ground_truth_subtrain,
        ground_truth_valid=ground_truth_valid,
        remove_already_bought=True,
        result_filepath=data_paths.result_filepath,
        start_epoch=fixed_params.start_epoch,
        patience=fixed_params.patience,
        pred=params['pred'],
        use_popularity=params['use_popularity'],
        weight_popularity=params['weight_popularity'],
        remove_false_negative=fixed_params.remove_false_negative,
        embedding_layer=params['embedding_layer'],
    )
    elapsed = time.time() - start_time
    result_to_save = f'\n {timedelta(seconds=elapsed)} \n END'
    save_txt(result_to_save, data_paths.result_filepath, mode='a')

    if visualization:
        plot_train_loss(hp_sentence, viz)

    # Report performance on validation set
    sentence = ("BEST VALIDATION Precision "
                "{:.3f}% | Recall {:.3f}% | Coverage {:.2f}%"
                .format(best_metrics['precision'] * 100,
                        best_metrics['recall'] * 100,
                        best_metrics['coverage'] * 100))

    log.info(sentence)
    save_txt(sentence, data_paths.result_filepath, mode='a')

    # Report performance on test set
    log.debug('Test metrics start ...')
    trained_model.eval()
    with torch.no_grad():
        embeddings = get_embeddings(valid_graph,
                                    params['out_dim'],
                                    trained_model,
                                    nodeloader_test,
                                    num_batches_test,
                                    cuda,
                                    device,
                                    params['embedding_layer'],
                                    )

        for ground_truth in [data.ground_truth_purchase_test, data.ground_truth_test]:
            precision, recall, coverage = get_metrics_at_k(
                embeddings,
                valid_graph,
                trained_model,
                params['out_dim'],
                ground_truth,
                all_eids_dict[('user', 'vote', 'item')],
                fixed_params.k,
                True,  # Remove already bought
                cuda,
                device,
                fixed_params.pred,
                params['use_popularity'],
                params['weight_popularity'],
            )

            sentence = ("TEST Precision "
                        "{:.3f}% | Recall {:.3f}% | Coverage {:.2f}%"
                        .format(precision * 100,
                                recall * 100,
                                coverage * 100))
            log.info(sentence)
            save_txt(sentence, data_paths.result_filepath, mode='a')

    if check_embedding:
        trained_model.eval()
        with torch.no_grad():
            log.debug('ANALYSIS OF RECOMMENDATIONS')
            # if 'sport' in train_graph.ntypes:
            #     result_sport = explore_sports(embeddings,
            #                                   data.sport_feat_df,
            #                                   data.spt_id,
            #                                   fixed_params.num_choices)

            #     save_txt(result_sport, data_paths.result_filepath, mode='a')

            already_bought_dict = create_already_bought(valid_graph,
                                                        all_eids_dict[('user', 'vote', 'item')],
                                                        )
            already_clicked_dict = None
            # if fixed_params.discern_clicks:
            #     already_clicked_dict = create_already_bought(valid_graph,
            #                                                  all_eids_dict[('user', 'clicks', 'item')],
            #                                                  etype='clicks',
            #                                                  )

            users, items = data.ground_truth_test
            ground_truth_dict = create_ground_truth(users, items)
            user_ids = np.unique(users).tolist()
            recs = get_recs(valid_graph,
                            embeddings,
                            trained_model,
                            params['out_dim'],
                            fixed_params.k,
                            user_ids,
                            already_bought_dict,
                            remove_already_bought=True,
                            pred=fixed_params.pred,
                            use_popularity=params['use_popularity'],
                            weight_popularity=params['weight_popularity'])

            users, items = data.ground_truth_purchase_test
            ground_truth_purchase_dict = create_ground_truth(users, items)
            explore_recs(recs,
                         already_bought_dict,
                         already_clicked_dict,
                         ground_truth_dict,
                         ground_truth_purchase_dict,
                         data.item_feat_df,
                         fixed_params.num_choices,
                         data.pdt_id,
                         fixed_params.item_id_type,
                         data_paths.result_filepath)

            if fixed_params.item_id_type == 'SPECIFIC ITEM_IDENTIFIER':
                coverage_metrics = check_coverage(data.user_item_train,
                                                  data.item_feat_df,
                                                  data.pdt_id,
                                                  recs)

                sentence = (
                    "COVERAGE \n|| All transactions : "
                    "Generic {:.1f}% | Junior {:.1f}% | Male {:.1f}% | Female {:.1f}% | Eco {:.1f}% "
                    "\n|| Recommendations : "
                    "Generic {:.1f}% | Junior {:.1f}% | Male {:.1f}% | Female {:.1f} | Eco {:.1f}%%"
                        .format(
                        coverage_metrics['generic_mean_whole'] * 100,
                        coverage_metrics['junior_mean_whole'] * 100,
                        coverage_metrics['male_mean_whole'] * 100,
                        coverage_metrics['female_mean_whole'] * 100,
                        coverage_metrics['eco_mean_whole'] * 100,
                        coverage_metrics['generic_mean_recs'] * 100,
                        coverage_metrics['junior_mean_recs'] * 100,
                        coverage_metrics['male_mean_recs'] * 100,
                        coverage_metrics['female_mean_recs'] * 100,
                        coverage_metrics['eco_mean_recs'] * 100,
                    )
                )
                log.info(sentence)
                save_txt(sentence, data_paths.result_filepath, mode='a')

        save_outputs(
            {
                'embeddings': embeddings,
                'already_bought': already_bought_dict,
                'already_clicked': already_bought_dict,
                'ground_truth': ground_truth_dict,
                'recs': recs,
            },
            'outputs/'
        )

        del params['remove']
        # Save model if the recall is greater than 8%
        if (recall > 0.08) & (fixed_params.item_id_type == 'SPECIFIC ITEM_IDENTIFIER') \
                or (recall > 0.2) & (fixed_params.item_id_type == 'GENERAL ITEM_IDENTIFIER'):
            date = str(datetime.datetime.now())[:-10].replace(' ', '')
            torch.save(trained_model.state_dict(), f'models/HP_Recall_{recall * 100:.2f}_{date}.pth')
            # Save all necessary params
            save_outputs(
                {
                    f'{date}_params': params,
                    f'{date}_fixed_params': vars(fixed_params),
                },
                'models/'
            )

        # Inference on different users
        if fixed_params.run_inference > 0:
            with torch.no_grad():
                print('On normal params')
                inference_recall = inference_hp.inference_fn(trained_model,
                                                             remove=fixed_params.remove_on_inference,
                                                             fixed_params=fixed_params,
                                                             overwrite_fixed_params=False,
                                                             **params)
                if fixed_params.run_inference > 1:
                    print('For all users')
                    del params['days_of_purchases'], params['days_of_clicks'], params['lifespan_of_items']
                    all_users_inference_recall = inference_hp.inference_fn(trained_model,
                                                                           remove=fixed_params.remove_on_inference,
                                                                           fixed_params=fixed_params,
                                                                           overwrite_fixed_params=True,
                                                                           days_of_purchases=710,
                                                                           days_of_clicks=710,
                                                                           lifespan_of_items=710,
                                                                           **params)

    recap = f"BEST RECALL on 1) Validation set : {best_metrics['recall'] * 100:.2f}%" \
            f'\n2) Test set : {recall * 100:.2f}%'
    if fixed_params.run_inference == 1:
        recap += f'\n3) On random users of {fixed_params.remove_on_inference} removed : {inference_recall * 100:.2f}'
    recap += f"\nLoop took {timedelta(seconds=elapsed)} for {len(viz['train_loss_list'])} epochs, an average of " \
             f"{timedelta(seconds=elapsed / len(viz['train_loss_list']))} per epoch"
    print(recap)
    save_txt(recap, data_paths.result_filepath, mode='a')

    return recall  # This is the 'test set' recall, on both purchases & clicks


class SearchableHyperparameters:
    """
    All hyperparameters to optimize.

    Attributes
    ----------
    Aggregator_hetero :
        How to aggregate messages from different types of edge relations. Choices : 'sum', 'max',
        'min', 'mean', 'stack'. More info here
        https://docs.dgl.ai/_modules/dgl/nn/pytorch/hetero.html
    Aggregator_type :
        How to aggregate neighborhood messages. Choices : 'mean', 'pool' for max pooling or 'lstm'
    Clicks_sample :
        Proportion of all clicks edges that should be used for training. Only relevant if
        fixed_params.train_on_clicks == True
    Days_popularity :
        Number of days considered in Use_popularity
    Dropout :
            Dropout used on nodes features (at all layers of the GNN)
    Embedding_layer :
        Create an explicit embedding layer that projects user & item features into and embedding
        of hidden_size dimension. If false, the embedding is done in the first layer of the GNN
        model.
    Purchases_sample :
        Proportion of all purchase (i.e. 'buys') edges that should be used for training. If
        fixed_params.discern_clicks == False, then 'clicks' edges are considered as 'purchases'
    Norm :
        Perform normalization after message aggregation
    Use_popularity :
        When computing ratings, add a score for items that were recent in the last X days
    Use_recency :
        When computing the loss, give more weights to more recent transactions
    Weight_popularity :
        Weight of the popularity score
    """
    def __init__(self):
        self.aggregator_hetero = Categorical(categories=['mean', 'sum', 'max'], name='aggregator_hetero')
        self.aggregator_type = Categorical(categories=['mean', 'mean_nn', 'pool_nn'], name='aggregator_type')  # LSTM?
        self.clicks_sample = Categorical(categories=[.2, .3, .4], name='clicks_sample')
        self.delta = Real(low=0.15, high=0.35, prior='log-uniform',
                          name='delta')
        self.dropout = Real(low=0., high=0.8, prior='uniform',
                            name='dropout')
        self.embed_dim = Categorical(categories=['Very Small', 'Small', 'Medium', 'Large', 'Very Large'],
                                     name='embed_dim')
        self.embedding_layer = Categorical(categories=[True, False], name='embedding_layer')
        self.lr = Real(low=1e-4, high=1e-2, prior='log-uniform', name='lr')
        self.n_layers = Integer(low=3, high=5, name='n_layers')
        self.neg_sample_size = Integer(low=700, high=3000,
                                       name='neg_sample_size')
        self.norm = Categorical(categories=[True, False], name='norm')
        self.popularity_importance = Categorical(categories=['No', 'Small', 'Medium', 'Large'],
                                                 name='popularity_importance')
        self.purchases_sample = Categorical(categories=[.4, .5, .6], name='purchases_sample')
        self.use_recency = Categorical(categories=[True, False], name='use_recency')

        # List all the attributes in a list.
        # This is equivalent to [self.hidden_dim_HP, self.out_dim_HP ...]
        self.dimensions = [self.__getattribute__(attr)
                           for attr in dir(self) if '__' not in attr]
        self.default_parameters = ['sum', 'mean_nn', .3, 0.266, .5, 'Medium', False,
                                   0.00565, 3, 2500, True, 'No', .5, True]


searchable_params = SearchableHyperparameters()
fitness_params = None

@use_named_args(dimensions=searchable_params.dimensions)
def fitness(**params):
    """
    Function used by skopt to find the best hyperparameter combination.

    The function calls the train function defined earlier, with all needed parameters. The recall that is returned
    is then multiplied by -1, since skopt is minimizing metrics.
    """
    recall = train(**{**fitness_params, **params})
    return -recall


@click.command()
@click.option('--from_beginning', count=True,
              help='Continue with last trained model or not')
@click.option('-v', '--verbose', count=True, help='Verbosity')
@click.option('-viz', '--visualization', count=True, help='Visualize result')
@click.option('--check_embedding', count=True, help='Explore embedding result')
@click.option('--remove', default=.95, help='Data remove percentage')
@click.option('--num_epochs', default=10, help='Number of epochs')
@click.option('--start_epoch', default=0, help='Start epoch')
@click.option('--patience', default=3, help='Patience for early stopping')
@click.option('--edge_batch_size', default=2048, help='Number of edges in a train / validation batch')
@click.option('--item_id_type', default='SPECIFIC ITEM IDENTIFIER',
              help='Identifier for the item. This code allows 2 types: SPECIFIC (e.g. item SKU'
                   'or GENERAL (e.g. item family)')
@click.option('--duplicates', default='keep_all',
              help='How to handle duplicates. Choices: keep_all, keep_last, count_occurrence')
def main(from_beginning, verbose, visualization, check_embedding,
         remove, num_epochs, start_epoch, patience, edge_batch_size,
         item_id_type, duplicates):
    """
    Main function that loads data and parameters, then runs hyperparameter loop with the fitness function.

    """
    if verbose:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)

    data_paths = DataPaths()
    fixed_params = FixedParameters(num_epochs, start_epoch, patience, edge_batch_size,
                                   remove, item_id_type, duplicates)

    checkpoint_saver = CheckpointSaver(
        f'checkpoint{str(datetime.datetime.now())[:-10]}.pkl',
        compress=9
    )

    data = DataLoader(data_paths, fixed_params)

    global fitness_params
    fitness_params = {
        'data': data,
        'fixed_params': fixed_params,
        'data_paths': data_paths,
        'visualization': visualization,
        'check_embedding': check_embedding,
    }
    if from_beginning:
        search_result = gp_minimize(
            func=fitness,
            dimensions=searchable_params.dimensions,
            n_calls=200,
            acq_func='EI',
            x0=searchable_params.default_parameters,
            callback=[checkpoint_saver],
            random_state=46,
            n_jobs=-1
        )

    if not from_beginning:
        checkpoint_path = None
        if checkpoint_path is None:
            checkpoint_path = get_last_checkpoint()
        res = load(checkpoint_path)

        x0 = res.x_iters
        y0 = res.func_vals

        search_result = gp_minimize(
            func=fitness,
            dimensions=searchable_params.dimensions,
            n_calls=200,
            n_initial_points=-len(x0),  # Workaround suggested to correct the error when resuming training
            acq_func='EI',
            x0=x0,
            y0=y0,
            callback=[checkpoint_saver],
            random_state=46
        )
    log.info(search_result)


if __name__ == '__main__':
    main()
