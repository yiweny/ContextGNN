from typing import Any, Dict, Optional, Type

import torch
from torch import Tensor
from torch_frame.data.stats import StatType
from torch_geometric.data import HeteroData
from torch_frame.nn.models import ResNet
from torch_geometric.nn import MLP
from torch_geometric.typing import NodeType

from hybridgnn.nn.encoder import (
    DEFAULT_STYPE_ENCODER_DICT,
    HeteroEncoder,
    HeteroTemporalEncoder,
)
from hybridgnn.nn.models import HeteroGraphSAGE
from torch_scatter import scatter_max
from torch_geometric.nn.aggr.utils import MultiheadAttentionBlock
from torch_geometric.utils import to_dense_batch
from torch_geometric.utils.map import map_index



class ReRankTransformer(torch.nn.Module):
    r"""Implementation of ReRank Transformer model.
    Args:
        data (HeteroData): dataset
        col_stats_dict (Dict[str, Dict[str, Dict[StatType, Any]]]): column stats
        num_nodes (int): number of nodes,
        num_layers (int): number of mp layers,
        channels (int): input dimension and embedding dimension
        aggr (str): aggregation type,
        norm (norm): normalization type,
        dropout (float): dropout rate for the transformer float,
        heads (int): number of attention heads,
        rank_topk (int): how many top results of gnn would be reranked,
        num_tr_layers (int): number of transformer layers,"""
    def __init__(
        self,
        data: HeteroData,
        col_stats_dict: Dict[str, Dict[str, Dict[StatType, Any]]],
        num_nodes: int,
        num_layers: int,
        channels: int,
        aggr: str = 'sum',
        norm: str = 'layer_norm',
        dropout: float = 0.2,
        heads: int = 1,
        rank_topk: int = 100, 
        t_encoding_type: str = "absolute",
        num_tr_layers: int = 1,
        torch_frame_model_cls: Type[torch.nn.Module] = ResNet,
        torch_frame_model_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()

        self.encoder = HeteroEncoder(
            channels=channels,
            node_to_col_names_dict={
                node_type: data[node_type].tf.col_names_dict
                for node_type in data.node_types
            },
            node_to_col_stats=col_stats_dict,
            stype_encoder_cls_kwargs=DEFAULT_STYPE_ENCODER_DICT,
            torch_frame_model_cls=torch_frame_model_cls,
            torch_frame_model_kwargs=torch_frame_model_kwargs,
        )
        self.temporal_encoder = HeteroTemporalEncoder(
            node_types=[
                node_type for node_type in data.node_types
                if "time" in data[node_type]
            ],
            channels=channels,
            encoding_type=t_encoding_type,
        )
        self.gnn = HeteroGraphSAGE(
            node_types=data.node_types,
            edge_types=data.edge_types,
            channels=channels,
            aggr=aggr,
            num_layers=num_layers,
        )
        self.head = MLP(
            channels,
            out_channels=1,
            norm=norm,
            num_layers=1,
        )
        self.lhs_projector = torch.nn.Linear(channels, channels)
        self.id_awareness_emb = torch.nn.Embedding(1, channels)
        self.rhs_embedding = torch.nn.Embedding(num_nodes, channels)
        self.lin_offset_idgnn = torch.nn.Linear(channels, 1)
        self.lin_offset_embgnn = torch.nn.Linear(channels, 1)

        self.rank_topk = rank_topk

        self.tr_embed_size = channels * 2      
        self.tr_blocks = torch.nn.ModuleList([
            MultiheadAttentionBlock(
                channels=self.tr_embed_size,
                heads=heads,
                layer_norm=True,
                dropout=dropout,
            ) for _ in range(num_tr_layers)
        ])
        self.tr_lin = torch.nn.Linear(self.tr_embed_size,1)

        self.channels = channels

        self.reset_parameters()

    def reset_parameters(self) -> None:
        self.encoder.reset_parameters()
        self.temporal_encoder.reset_parameters()
        self.gnn.reset_parameters()
        self.head.reset_parameters()
        self.id_awareness_emb.reset_parameters()
        self.rhs_embedding.reset_parameters()
        self.lin_offset_embgnn.reset_parameters()
        self.lin_offset_idgnn.reset_parameters()
        self.lhs_projector.reset_parameters()
        for block in self.tr_blocks:
            block.reset_parameters()
        self.tr_lin.reset_parameters()

    def forward(
        self,
        batch: HeteroData,
        entity_table: NodeType,
        dst_table: NodeType,
        dst_entity_col: NodeType,
    ) -> Tensor:
     
        seed_time = batch[entity_table].seed_time
        x_dict = self.encoder(batch.tf_dict)

        # Add ID-awareness to the root node
        x_dict[entity_table][:seed_time.size(0
                                             )] += self.id_awareness_emb.weight
        rel_time_dict = self.temporal_encoder(seed_time, batch.time_dict,
                                              batch.batch_dict)

        for node_type, rel_time in rel_time_dict.items():
            x_dict[node_type] = x_dict[node_type] + rel_time

        x_dict = self.gnn(
            x_dict,
            batch.edge_index_dict,
        )

        batch_size = seed_time.size(0)
        lhs_embedding = x_dict[entity_table][:
                                             batch_size]  # batch_size, channel
        lhs_embedding_projected = self.lhs_projector(lhs_embedding)
        rhs_gnn_embedding = x_dict[dst_table]  # num_sampled_rhs, channel
        rhs_idgnn_index = batch.n_id_dict[dst_table]  # num_sampled_rhs
        lhs_idgnn_batch = batch.batch_dict[dst_table]  # batch_size

        rhs_embedding = self.rhs_embedding  # num_rhs_nodes, channel
        embgnn_logits = lhs_embedding_projected @ rhs_embedding.weight.t(
        )  # batch_size, num_rhs_nodes

        # Model the importance of embedding-GNN prediction for each lhs node
        embgnn_offset_logits = self.lin_offset_embgnn(
            lhs_embedding_projected).flatten()
        embgnn_logits += embgnn_offset_logits.view(-1, 1)

        # Calculate idgnn logits
        idgnn_logits = self.head(
            rhs_gnn_embedding).flatten()  # num_sampled_rhs
        
        # Because we are only doing 2 hop, we are not really sampling info from
        # lhs therefore, we need to incorporate this information using
        # lhs_embedding[lhs_idgnn_batch] * rhs_gnn_embedding
        idgnn_logits += (
            lhs_embedding[lhs_idgnn_batch] *  # num_sampled_rhs, channel
            rhs_gnn_embedding).sum(
                dim=-1).flatten()  # num_sampled_rhs, channel

        # Model the importance of ID-GNN prediction for each lhs node
        idgnn_offset_logits = self.lin_offset_idgnn(
            lhs_embedding_projected).flatten()
        idgnn_logits = idgnn_logits + idgnn_offset_logits[lhs_idgnn_batch]

        embgnn_logits[lhs_idgnn_batch, rhs_idgnn_index] = idgnn_logits

        shallow_rhs_embed = rhs_embedding.weight

        _, original_indices = torch.topk(embgnn_logits, self.rank_topk, dim=1)

        transformer_logits, topk_index = self.rerank(embgnn_logits, shallow_rhs_embed, rhs_gnn_embedding, rhs_idgnn_index, idgnn_logits, lhs_idgnn_batch,lhs_embedding_projected[lhs_idgnn_batch])

        assert torch.equal(original_indices, topk_index)
        return embgnn_logits, transformer_logits, topk_index


    def rerank(self, gnn_logits, shallow_rhs_embed, rhs_idgnn_embed, rhs_idgnn_index, idgnn_logits, lhs_idgnn_batch, lhs_embedding):
        """
        reranks the gnn logits based on the provided gnn embeddings. 
        shallow_rhs_embed:[# rhs nodes, embed_dim]
        """
        embed_size = rhs_idgnn_embed.shape[1]
        batch_size = gnn_logits.shape[0]
        num_rhs_nodes = shallow_rhs_embed.shape[0]
        
        filtered_logits, topk_indices = torch.topk(gnn_logits, self.rank_topk, dim=1)
        out_indices = topk_indices.clone()
        # [batch_size, topk, embed_size]
        seq = shallow_rhs_embed[topk_indices.flatten()].view(batch_size * self.rank_topk, embed_size) 

        query_rhs_idgnn_index, mask = map_index(topk_indices.view(-1), rhs_idgnn_index)
        id_gnn_seq = torch.zeros(batch_size * self.rank_topk, embed_size).to(rhs_idgnn_embed.device)
        id_gnn_seq[mask] = rhs_idgnn_embed[query_rhs_idgnn_index]

        logit_mask = torch.zeros(batch_size * self.rank_topk, embed_size, dtype=bool).to(rhs_idgnn_embed.device)
        logit_mask[mask] = True
        seq = torch.where(logit_mask, id_gnn_seq.view(-1,embed_size), seq.view(-1,embed_size))

        lhs_uniq_embed = lhs_embedding[:batch_size]

        # seq = seq.clone()
        seq = seq.view(batch_size,self.rank_topk,-1)

        lhs_uniq_embed = lhs_uniq_embed.view(-1,1,embed_size)
        lhs_uniq_embed = lhs_uniq_embed.expand(-1,seq.shape[1],-1)
        seq = torch.cat((seq,lhs_uniq_embed), dim=-1)

        for block in self.tr_blocks:
            seq = block(seq, seq) # [# nodes, topk, embed_size]

        #! just get the logit directly from transformer
        seq = seq.reshape(-1,self.tr_embed_size)
        tr_logits = self.tr_lin(seq) # [batch_size, embed_size]
        tr_logits = tr_logits.view(batch_size,self.rank_topk)

        # seq = seq.view(batch_size * self.rank_topk, embed_size)
        # lhs_uniq_embed = lhs_uniq_embed.reshape(batch_size * self.rank_topk, embed_size)

        # tr_logits = (lhs_uniq_embed.view(-1, embed_size) * seq.view(-1, embed_size)).sum(
        #         dim=-1).flatten()

        return tr_logits, out_indices

        


